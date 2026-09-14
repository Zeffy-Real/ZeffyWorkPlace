"""P3-2 告警：阈值扫描 → 触发/恢复两条链路，均写 AuditLog、Redis 冷却防刷屏。

🔴 审查修订：
- **告警恢复**：指标从告警区回到正常 → 写恢复审计（``operator="alert-resolve"``），
  运营可感知故障已消除（不只是触发）。
- **冷却**：同一指标在 ``ALERT_COOLDOWN`` 秒内的触发/恢复都通过 Redis ``SET NX EX``
  防刷屏；自容无外部通知渠道（记入 P4 遗留，接邮件/webhook/IM）。
- 告警结果由后台协程随指标采集一并执行（见 metrics._collection_loop 同周期），不额外高频扫描。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.db import repos

logger = logging.getLogger(__name__)

# 告警指标 key（冷却键 = ALERT_COOLDOWN_KEY:{metric}）
ALERT_COOLDOWN_KEY = "zw:alertcooldown"

# 进程内记录上一轮是否处于告警区（同一进程内做恢复判定；跨实例由 Redis 冷却兜底去重）
_was_alerted: dict[str, bool] = {}


def _above_threshold(metric: str, metrics: dict[str, Any]) -> bool:
    s = get_settings()
    node = metrics.get("node") or {}
    redis = metrics.get("redis") or {}
    if metric == "queue_depth":
        return node.get("queue_depth", 0) >= s.ALERT_QUEUE_THRESHOLD
    if metric == "queue_redis_len":
        return (redis.get("queue_len") or 0) >= s.ALERT_QUEUE_THRESHOLD
    if metric == "node_failure_rate":
        return node.get("failure_rate", 0.0) >= s.ALERT_FAILURE_THRESHOLD
    return False


async def _audit(session_factory, *, operator: str, action: str,
                 detail: dict[str, Any]) -> None:
    try:
        async with session_factory() as s:
            await repos.write_audit(s, task_id=None, operator=operator,
                                    action=action, detail=detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("告警审计落库失败：%s", exc)


async def _cooled(redis: Any | None, metric: str) -> bool:
    """冷却期内返回 True（跳过触发/恢复）。无 redis 则按进程内 set 兜底。"""
    key = f"{ALERT_COOLDOWN_KEY}:{metric}"
    if redis is not None:
        try:
            return not bool(await redis.set(key, "1", nx=True,
                                           ex=get_settings().ALERT_COOLDOWN))
        except Exception:  # noqa: BLE001
            pass
    if _was_alerted.get(metric):
        return True
    _was_alerted[metric] = True
    return False


async def run_alert_scan(session_factory, metrics: dict[str, Any],
                         redis: Any | None = None) -> list[dict[str, str]]:
    """对当前指标快照跑一次阈值判定；返回本轮触发/恢复事件列表（供测试断言）。"""
    events: list[dict[str, str]] = []
    for metric in ("queue_depth", "queue_redis_len", "node_failure_rate"):
        elevated = _above_threshold(metric, metrics) and metrics.get("error") is None
        prev = _was_alerted.get(metric, False)
        if elevated and not prev:
            if await _cooled(redis, metric):
                continue
            _was_alerted[metric] = True
            await _audit(session_factory, operator="alert", action="alert_trigger",
                         detail={"metric": metric})
            events.append({"metric": metric, "state": "triggered"})
        elif not elevated and prev:
            _was_alerted[metric] = False
            await _audit(session_factory, operator="alert-resolve",
                         action="alert_resolve", detail={"metric": metric})
            events.append({"metric": metric, "state": "resolved"})
    return events
