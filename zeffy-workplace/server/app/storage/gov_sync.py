"""P6-4-B 灰度中心化 · 同步守护协程。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_sub_task: asyncio.Task | None = None
_full_lock: asyncio.Lock | None = None
_last_full_ts = 0.0


async def _full_sync_once(force: bool = False) -> dict:
    """全量同步一次（按 min-interval 节流；force=重连时允许穿越节流）。"""
    global _last_full_ts, _full_lock
    from app.storage import governance as govm

    if _full_lock is None:
        _full_lock = asyncio.Lock()
    now = time.monotonic()
    min_iv = max(1.0, get_settings().GOV_SYNC_MIN_INTERVAL)
    if not force and (_last_full_ts and (now - _last_full_ts) < min_iv):
        return {"throttled": True}
    async with _full_lock:
        _last_full_ts = time.monotonic()
        return await govm.gov_load_all_into_cache()


async def _handle_invalidated(data: dict[str, Any]) -> None:
    """处理一条失效广播：打散延迟后回源重载该单项（审查 🔴5 防风暴 + 🔴2 权威校验）。"""
    from app.storage import governance as govm

    kind = data.get("type")
    key = data.get("key")
    ver = int(data.get("ver") or 0)
    if kind not in ("ovr", "gray") or not key:
        return
    # 失效拉取加随机延迟，打散各实例同时打 Redis 的尖峰
    await asyncio.sleep(random.uniform(0, max(0, get_settings().GOV_FADE_MAX_MS) / 1000.0))
    store = govm._get_gov_store()
    if store is None:
        return
    try:
        if kind == "ovr":
            v, val = await store.load_override(key)
            govm._apply_ovr_cache(key, v if v is not None else ver, val)
        else:
            v, members = await store.load_gray_members(key)
            govm._apply_gray_cache(key, v if v is not None else ver, members)
    except Exception as exc:  # noqa: BLE001 单条重载失败由定期校验兜底
        logger.warning("灰度失效重载失败 kind=%s key=%s: %s", kind, key, exc)


async def _subscriber(redis: Any, channel: str) -> None:
    """订阅；建立（含每次重连）后强制全量同步一次，弥补断连期丢消息。"""
    s = get_settings()
    while True:
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            logger.info("灰度中心化订阅建立 channel=%s", channel)
            await _full_sync_once(force=True)  # 重连/启动：全量追平
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    await _handle_invalidated(json.loads(msg.get("data")))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("灰度广播处理失败：%s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 断线 → 指数退避重连
            logger.warning("灰度中心化订阅异常，将重连：%s", exc)
            try:
                await pubsub.unsubscribe(channel)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(min(30, max(1, s.GOV_SYNC_MIN_INTERVAL)))
            continue


async def _periodic(redis: Any, channel: str) -> None:
    """定期全量校验（最终一致兜底，审查 🔴2）。"""
    interval = max(60, get_settings().GOV_SYNC_INTERVAL)
    while True:
        await asyncio.sleep(interval)
        try:
            await _full_sync_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("灰度定期校验失败：%s", exc)


def start_gov_sync(redis: Any | None = None, channel: str | None = None) -> None:
    """启动同步守护（GOV_CENTRALIZE 开启时）。幂等。"""
    global _sub_task
    s = get_settings()
    if not s.GOV_CENTRALIZE or redis is None:
        return
    if _sub_task is not None and not _sub_task.done():
        return
    ch = channel or s.GOV_REDIS_CHANNEL
    _sub_task = asyncio.create_task(_subscriber(redis, ch))
    _sub_task.add_done_callback(_log_done)


def _log_done(t: asyncio.Task) -> None:
    if not t.cancelled():
        try:
            t.result()
        except Exception:  # noqa: BLE001
            logger.exception("灰度同步协程退出")


async def stop_gov_sync() -> None:
    """停止同步守护。"""
    global _sub_task
    if _sub_task is not None:
        _sub_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
            await _sub_task
        _sub_task = None


async def prewarm() -> dict:
    """启动预热：服务接客前先全量加载一次（审查 🔴1 启动原子）。"""
    return await _full_sync_once(force=True)
