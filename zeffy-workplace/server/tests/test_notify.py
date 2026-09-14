"""P4-2 通知：内容白名单 / 冷却 / 风暴抑制 / 失败重试与审计。

对 ``dispatch_one`` 做纯函数验证（注入 fake notifier + fakeredis）。
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.init_db import init_db
from app.observability import notify


class FakeNotifier(notify.Notifier):
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail: bool = False

    async def send(self, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(payload)


def _rules(*rules: dict) -> list[dict]:
    s = get_settings()
    s.NOTIFY_RULES = list(rules)
    return s.NOTIFY_RULES


def _ev(metric="queue_depth", state="triggered") -> notify.NotificationEvent:
    return notify.NotificationEvent(metric=metric, level="warning", state=state,
                                    threshold="20", instance_id="i-1",
                                    triggered_at="2026-01-01T00:00:00Z")


async def _factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    return async_sessionmaker(eng, expire_on_commit=False), eng


@pytest.mark.asyncio
async def test_payload_whitelist_only():
    """🔴 内容白名单：payload 不含业务/内部字段。"""
    _rules({"metric": "queue_depth", "level": "warning", "channel": "webhook"})
    f = FakeNotifier()
    await notify.dispatch_one(_ev(), {"webhook": [f]}, redis=fakeredis.aioredis.FakeRedis())
    assert f.sent, "应发送一条"
    p = f.sent[0]
    assert set(p.keys()) <= {"type", "metric", "level", "state", "threshold",
                             "instance_id", "triggered_at"}
    get_settings().NOTIFY_RULES = []


@pytest.mark.asyncio
async def test_no_rules_no_notify():
    get_settings().NOTIFY_RULES = []
    f = FakeNotifier()
    r = await notify.dispatch_one(_ev(), {"webhook": [f]}, redis=fakeredis.aioredis.FakeRedis())
    assert f.sent == [] and r == {"sent": 0, "skipped_cooldown": 0, "skipped_rate": 0, "failed": 0}


@pytest.mark.asyncio
async def test_cooldown_dedup_trigger_and_resolve_separate():
    """🔴 触发与恢复各自独立冷却：同 state 冷却期内不再发；不同 state 各发一次。"""
    s = get_settings()
    s.NOTIFY_RULES = [{"metric": "queue_depth", "level": "warning", "channel": "webhook"}]
    s.ALERT_COOLDOWN = 300
    r = fakeredis.aioredis.FakeRedis()
    f = FakeNotifier()
    ns = {"webhook": [f]}
    await notify.dispatch_one(_ev("queue_depth", "triggered"), ns, redis=r)
    await notify.dispatch_one(_ev("queue_depth", "triggered"), ns, redis=r)  # 冷却内→跳过
    await notify.dispatch_one(_ev("queue_depth", "resolved"), ns, redis=r)  # 恢复独立→发
    assert len(f.sent) == 2, f"触发1+恢复1=2，实际{len(f.sent)}"
    states = sorted(p["state"] for p in f.sent)
    assert states == ["resolved", "triggered"]
    get_settings().NOTIFY_RULES = []
    s.ALERT_COOLDOWN = 300


@pytest.mark.asyncio
async def test_storm_rate_cap_and_audit():
    """🔴 单通道每分钟上限 → 超限跳过 + 写 notify_storm 审计。"""
    s = get_settings()
    s.NOTIFY_RULES = [
        {"metric": "queue_depth", "level": "warning", "channel": "webhook"},
        {"metric": "node_failure_rate", "level": "warning", "channel": "webhook"},
        {"metric": "queue_redis_len", "level": "warning", "channel": "webhook"},
    ]
    s.NOTIFY_MAX_PER_MIN = 2
    factory, eng = await _factory()
    r = fakeredis.aioredis.FakeRedis()
    f = FakeNotifier()
    ns = {"webhook": [f]}
    # 三个不同 metric（各自冷却键独立不冲突），同一通道同分钟累加 rate
    await notify.dispatch_one(_ev("queue_depth", "triggered"), ns, redis=r,
                              session_factory=factory)
    await notify.dispatch_one(_ev("node_failure_rate", "triggered"), ns, redis=r,
                              session_factory=factory)
    res3 = await notify.dispatch_one(_ev("queue_redis_len", "triggered"), ns, redis=r,
                                     session_factory=factory)
    assert res3["skipped_rate"] >= 1, "第3条应被速率上限拦截"
    # 风暴审计落库
    from app.db import repos

    async with factory() as ss:
        logs = await repos.list_audit(ss, action="notify_storm")
    assert logs, "应写 notify_storm 审计"
    get_settings().NOTIFY_RULES = []
    s.NOTIFY_MAX_PER_MIN = 30
    await eng.dispose()


@pytest.mark.asyncio
async def test_retry_then_fail_audits():
    """发送持续失败 → 重试用尽 → 写 notify_failed，不抛出。"""
    s = get_settings()
    s.NOTIFY_RULES = [{"metric": "queue_depth", "level": "warning", "channel": "webhook"}]
    s.NOTIFY_RETRIES = 1
    s.NOTIFY_BACKOFF_BASE = 0.01
    factory, eng = await _factory()
    f = FakeNotifier()
    f.fail = True
    res = await notify.dispatch_one(_ev(), {"webhook": [f]},
                                    redis=fakeredis.aioredis.FakeRedis(),
                                    session_factory=factory)
    assert res["failed"] == 1 and res["sent"] == 0
    from app.db import repos

    async with factory() as ss:
        logs = await repos.list_audit(ss, action="notify_failed")
    assert logs
    get_settings().NOTIFY_RULES = []
    s.NOTIFY_RETRIES = 3
    await eng.dispose()
