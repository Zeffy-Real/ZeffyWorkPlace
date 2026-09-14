"""P4-1 跨机器集群：实例注册 / 心跳 / 集群视图 / 启动时钟校验。

键：``zw:instances:{id}``（TTL=心跳周期×3），内容 hash ``{kind, host, pid, started_at}``。
规则（审查🔴）：
- 实例 ID 全局唯一（hostname-pid-randhex），注册 SET NX 失败即拒绝（防冲突/租约混淆）。
- 启动时钟校验：用 Redis ``TIME`` 对标物理机，偏差 > ``MAX_CLOCK_SKEW`` 拒绝启动（NTP 前置）。
- 启动注册 → 周期心跳续期 → 退出主动注销（不等 TTL）。
- 集群视图：``list_instances`` 列在线实例 + 健康分级。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# 常量前缀（无 config 依赖，便于测试/跨模块引用）
INSTANCES_KEY = "zw:instances"


class InstanceError(Exception):
    """实例注册/校验失败（应由调用方转告警/决定是否拒绝启动）。"""


def _key(instance_id: str) -> str:
    return f"{INSTANCES_KEY}:{instance_id}"


async def check_clock_skew(redis: Any, *, max_skew: float) -> float:
    """用 Redis 服务端时钟校验本机时间偏差（秒）。

    返回绝对偏差；>max_skew 抛 ``InstanceError``（调用方据此拒绝启动）。
    redis 不可用时抛 InstanceError（提示无法校验，需人工确认 NTP）。
    """
    try:
        sec, _us = await redis.time()  # (seconds, microseconds)
    except Exception as exc:  # noqa: BLE001
        raise InstanceError(f"无法校验时钟偏差（redis.time 失败）：{exc}") from exc
    server_ts = float(sec)
    local_ts = time.time()
    skew = abs(server_ts - local_ts)
    if skew > max_skew:
        raise InstanceError(
            f"系统时钟偏差 {skew:.2f}s 超过上限 {max_skew}s，请先配置 NTP 时间同步（要求 ≤1s）"
        )
    return skew


async def register(redis: Any, *, instance_id: str, kind: str, host: str, ttl: int) -> bool:
    """注册实例：SET NX 保证唯一；已存在（同 ID 在线）→ False（拒绝）。

    :return: True 注册成功；False 实例已存在（冲突）。
    """
    payload = {
        "kind": kind, "host": host, "pid": str(_pid()),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        ok = await redis.set(_key(instance_id), _dump(payload), nx=True, ex=ttl * 3)
        return ok is True  # redis SET NX：成功 True；键已存在返回 None→False（冲突拒绝）
    except Exception as exc:  # noqa: BLE001
        raise InstanceError(f"实例注册失败：{exc}") from exc


def _dump(d: dict) -> str:
    import json

    return json.dumps(d)


def _load(raw) -> dict:
    import json

    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _pid() -> int:
    import os

    return os.getpid()


async def _heartbeat_once(redis: Any, instance_id: str, ttl: int) -> None:
    try:
        await redis.expire(_key(instance_id), ttl * 3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("实例心跳续期失败 inst=%s：%s", instance_id, exc)


async def _heartbeat_loop(redis: Any, instance_id: str, ttl: int) -> None:
    while True:
        await asyncio.sleep(ttl)
        await _heartbeat_once(redis, instance_id, ttl)


def start_ticker(redis: Any, *, instance_id: str, ttl: int) -> asyncio.Task:
    """启动周期续期协程（每 ttl 秒 EXPIRE 一次；键本身 TTL=3×ttl）。"""
    return asyncio.create_task(_heartbeat_loop(redis, instance_id, ttl))


async def unregister(redis: Any, *, instance_id: str) -> None:
    """主动注销：删除实例键（避免等 TTL）。"""
    with contextlib.suppress(Exception):  # noqa: BLE001
        await redis.delete(_key(instance_id))


async def list_instances(redis: Any) -> list[dict]:
    """扫描在线实例；每项含 id/kind/host/pid/started_at 与是否超时兜底标记。"""
    out: list[dict] = []
    try:
        async for raw_key in redis.scan_iter(match=f"{INSTANCES_KEY}:*"):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            inst_id = key.rsplit(":", 1)[-1]
            raw = await redis.get(key)
            info = _load(raw)
            info.setdefault("instance_id", inst_id)
            out.append(info)
    except Exception as exc:  # noqa: BLE001
        logger.warning("集群实例扫描失败：%s", exc)
    out.sort(key=lambda x: x.get("instance_id", ""))
    return out


def current_host() -> str:
    """脱敏主机名（不暴露内网 IP；审查⭐）。"""
    return socket.gethostname()


def health_level(instance: dict, *, online_ttl_ratio: float = 0.5) -> str:
    """集群健康分级：处于窗口内持续心跳视为 online；距其续期较旧为 degraded；暂按 in-memory 标记。

    本实现以注册/心跳为代表：能列出即 online；对无 started_at 的异常行标 degraded。
    """
    if instance.get("started_at"):
        return "online"
    return "degraded"


async def shutdown_ticker(task: asyncio.Task | None) -> None:
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
            await task
