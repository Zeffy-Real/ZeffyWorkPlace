"""P3-2 worker 心跳：worker 周期写 ``zw:workers:{id}``，TTL 决定离线识别。

指标采集用 ``SCAN zw:workers:*`` 统计活跃 worker 数（active_workers）。
Worker on_startup 注册 → 周期续期；on_shutdown 主动注销（否则等 TTL 才识别离线）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

KEY_PREFIX = "zw:workers"


async def heartbeat(redis: Any, *, worker_id: str, ttl: int = 90) -> None:
    """写一次心跳。失败仅告警（下轮重试；TTL 兜底）。"""
    try:
        await redis.set(f"{KEY_PREFIX}:{worker_id}", "1", ex=ttl)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("worker 心跳写入失败：%s", exc)


async def _heartbeat_loop(redis: Any, *, worker_id: str, ttl: int = 90) -> None:
    while True:
        await asyncio.sleep(ttl / 3)
        await heartbeat(redis, worker_id=worker_id, ttl=ttl)


def start_heartbeat(redis: Any, *, worker_id: str, ttl: int = 90) -> asyncio.Task:
    """启动心跳后台协程（worker 进程内调用）。"""
    return asyncio.create_task(_heartbeat_loop(redis, worker_id=worker_id, ttl=ttl))


async def unregister(redis: Any, *, worker_id: str) -> None:
    """主动注销：删除心跳键（避免等待 TTL）。"""
    with contextlib.suppress(Exception):  # noqa: BLE001
        await redis.delete(f"{KEY_PREFIX}:{worker_id}")
