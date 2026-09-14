"""P3-2 可观测：后台定时采集 DB + Redis → 内存快照；/metrics 返回缓存，杜绝高频查库。

采集项（指标清单）：
- ``tasks`` / ``nodes``：按状态分组计数（全量快照）
- ``throughput``：统计窗口内完成节点数（执行吞吐）
- ``queue_depth`` / ``queue_age_avg``：滞留 queued 节点数 + 队首滞留平均秒数
- ``queue_redis_len``：ARQ 队列 Redis ``LLEN``（redis 不可用时置 None）
- ``node_failure_rate``：窗口内失败率
- ``active_workers``：Redis ``SCAN zw:workers:*`` 存活心跳数

🔴 /metrics 只读缓存（self.get_metrics）：由后台协程按 ``METRICS_INTERVAL`` 刷新，
避免每次调用实时 GROUP BY 打挂存储。DB 采集失败时返回上次快照 + error 标记。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.db import repos
from app.db.models import Task, TaskNode
from app.observability import alerts

logger = logging.getLogger(__name__)

_snapshot: dict[str, Any] = {
    "collected_at": None,
    "error": "metrics not collected yet",
}
_collect_task: asyncio.Task | None = None


def get_metrics() -> dict[str, Any]:
    """返回最近一次采集快照（缓存）；未采集时含 error 说明。"""
    return _snapshot


async def collect_metrics(session_factory, redis: Any | None = None) -> dict[str, Any]:
    """采集一次 DB + Redis 快照并写入缓存。任何单点采集失败不阻断整体（分字段兜底）。"""
    payload: dict[str, Any] = {
        "collected_at": datetime.now(UTC).isoformat(),
        "node": {},
        "task": {},
        "redis": None,
        "worker": {"active": 0, "ids": []},
    }
    now = datetime.now(UTC)
    since = now - timedelta(minutes=get_settings().METRICS_TREND_MINUTES)
    db_ok = False
    try:
        async with session_factory() as s:
            task_counts = await repos.count_by_status(s, model=Task,
                                                      status_col=Task.status)
            node_counts = await repos.count_by_status(s, model=TaskNode,
                                                      status_col=TaskNode.status)
            depth, age = await repos.queued_depth_age(s)
            fail_rate = await repos.node_failure_rate(s, since=since)
        db_ok = True
    except Exception as exc:  # noqa: BLE001 DB 挂了也要暴露（/health 用于分级）
        logger.warning("指标 DB 采集失败：%s", exc)
        _snapshot["error"] = f"db: {exc}"
        payload["error"] = f"db: {exc}"

    if db_ok:
        payload["task"] = {
            "total": sum(task_counts.values()),
            "by_status": task_counts,
        }
        payload["node"] = {
            "by_status": node_counts,
            "throughput": node_counts.get("done", 0),
            "queue_depth": depth,
            "queue_age_avg_sec": round(age, 2),
            "failure_rate": round(fail_rate, 4),
        }

    # Redis 侧（LLEN / SCAN workers）；不可用则跳过（不阻断）
    if redis is not None:
        try:
            queue_len = 0
            per_queue: dict[str, int] = {}
            from app.queue.priorities import all_queues

            for qname in sorted(all_queues()):
                n = int(await redis.llen(qname) or 0)
                per_queue[qname] = n
                queue_len += n
            payload["redis"] = {"queue_len": queue_len, "per_queue": per_queue}

            workers: list[str] = []
            async for key in redis.scan_iter(match="zw:workers:*"):
                workers.append(key.decode() if isinstance(key, bytes) else str(key))
            workers.sort()
            payload["worker"] = {
                "active": len(workers),
                "ids": [w.rsplit(":", 1)[-1] for w in workers],
            }

            # P4-1：集群实例视图（kind 计数）
            from app.observability import instance_reg

            insts = await instance_reg.list_instances(redis)
            counts: dict[str, int] = {}
            for i in insts:
                k = i.get("kind", "unknown")
                counts[k] = counts.get(k, 0) + 1
            payload["cluster"] = {
                "instances": len(insts),
                "by_kind": counts,
                "ids": [i.get("instance_id", "") for i in insts],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("指标 redis 采集失败：%s", exc)
            payload["redis"] = None
            payload["worker_error"] = str(exc)

    _snapshot.clear()
    _snapshot.update(payload)
    return payload


async def run_monitor_tick(session_factory, redis: Any | None = None) -> list[dict[str, str]]:
    """后台单轮巡检：采集指标 → 阈值告警（触发/恢复）。返回本轮告警事件（供测试断言）。

    🔴 /metrics 只读缓存（get_metrics）；本函数只由后台协程调用，不承载 HTTP 高频请求。
    """
    snap = await collect_metrics(session_factory, redis=redis)
    return await alerts.run_alert_scan(session_factory, snap, redis=redis)


async def _collection_loop(session_factory, redis: Any | None = None) -> None:
    """后台协程：按 METRICS_INTERVAL 周期采集。"""
    interval = max(10, get_settings().METRICS_INTERVAL)
    while True:
        await asyncio.sleep(interval)
        with contextlib.suppress(Exception):  # noqa: BLE001 采集失败下轮重试
            await collect_metrics(session_factory, redis=redis)


def start_collector(session_factory, redis: Any | None = None) -> None:
    """在 lifespan 启动采集后台协程（返回前缓存已就绪一次）。"""
    global _collect_task
    if _collect_task is not None and not _collect_task.done():
        return
    _collect_task = asyncio.create_task(_collection_loop(session_factory, redis=redis))


async def stop_collector() -> None:
    """回收采集协程。"""
    global _collect_task
    if _collect_task is not None:
        _collect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
            await _collect_task
        _collect_task = None


async def _monitor_loop(session_factory, redis: Any | None = None) -> None:
    """周期：采集指标 + 告警触发/恢复；P4-2 把告警事件异步投递到外部通知。"""
    interval = max(10, get_settings().METRICS_INTERVAL)
    while True:
        await asyncio.sleep(interval)
        try:
            events = await run_monitor_tick(session_factory, redis=redis)
        except Exception:  # noqa: BLE001 单轮失败下轮重试
            events = []
        if events:
            from app.observability import notify

            with contextlib.suppress(Exception):  # noqa: BLE001 通知失败不影响监控
                await notify.enqueue_alert_events(events)


_monitor_task: asyncio.Task | None = None


def start_monitor(session_factory, redis: Any | None = None) -> None:
    """lifespan 统一启停监控（含指标 + 告警），与 USE_QUEUE 解耦。"""
    global _monitor_task
    if _monitor_task is not None and not _monitor_task.done():
        return
    _monitor_task = asyncio.create_task(_monitor_loop(session_factory, redis=redis))


async def stop_monitor() -> None:
    global _monitor_task
    if _monitor_task is not None:
        _monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
            await _monitor_task
        _monitor_task = None
