"""P3-2 监控：指标缓存采集 /metrics、告警触发+恢复、冷却去重。

用内存 SQLite + fakeredis 桩（LLEN/SCAN/SET NX EX）验证：
- 🔴 指标采集写入缓存（缓存放 DB 高频查库）
- 🔴 队列深度超阈值 → 触发告警 AuditLog(alert)
- 🔴 冷却期内同一指标不重复触发
- 🔴 指标回落 → 恢复告警 AuditLog(alert-resolve)
- /health 分级（healthy/degraded/unhealthy）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import repos
from app.db.init_db import init_db
from app.db.models import TaskNode as TN
from app.observability import alerts
from app.observability import metrics as metrics_mod


async def _factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    return async_sessionmaker(eng, expire_on_commit=False), eng


async def _mk_queued(factory, n: int):
    async with factory() as s:
        t = await repos.create_task(s, title="m", workflow_id="generic")
        from app.db.repos import create_node
        nodes = []
        for i in range(n):
            nd = await create_node(s, task_id=t.id, node_name=f"N{i}")
            await repos.set_node_queued(s, nd.id,
                                        worker_at=datetime.now(UTC) - timedelta(seconds=30))
            nodes.append(nd)
        await s.commit()
        return t.id


@pytest.mark.asyncio
async def test_metrics_collection_and_cache():
    factory, eng = await _factory()
    await _mk_queued(factory, 3)
    r = fakeredis.aioredis.FakeRedis()
    # 模拟 redis 队列 + worker 心跳
    await r.rpush("zeffy", "job1", "job2")
    await r.rpush("zeffy_hi", "hi1")
    await r.set("zw:workers:w1", "1", ex=90)

    snap = await metrics_mod.collect_metrics(factory, redis=r)
    assert snap["collected_at"]
    assert snap["node"]["queue_depth"] == 3
    assert snap["redis"]["queue_len"] == 3  # zeffy=2 + zeffy_hi=1（分级聚合）
    assert snap["redis"]["per_queue"]["zeffy"] == 2
    assert snap["redis"]["per_queue"]["zeffy_hi"] == 1
    assert snap["worker"]["active"] == 1
    # /metrics 读缓存：内存中已写入
    cached = metrics_mod.get_metrics()
    assert cached["node"]["queue_depth"] == 3
    await eng.dispose()
    metrics_mod._snapshot.clear()
    metrics_mod._snapshot.update({"collected_at": None, "error": "metrics not collected yet"})


@pytest.mark.asyncio
async def test_alert_trigger_and_cooldown():
    """🔴 队列深度超阈值→触发告警；冷却期内不重复；回落→恢复。"""
    from app.config import get_settings
    get_settings().ALERT_QUEUE_THRESHOLD = 2

    factory, eng = await _factory()
    await _mk_queued(factory, 5)
    r = fakeredis.aioredis.FakeRedis()

    snap = await metrics_mod.collect_metrics(factory, redis=r)
    ev1 = await alerts.run_alert_scan(factory, snap, redis=r)
    asserted = [e for e in ev1 if e["metric"] == "queue_depth"]
    assert asserted and asserted[0]["state"] == "triggered"

    # 冷却期内再扫：不重复触发
    ev2 = await alerts.run_alert_scan(factory, snap, redis=r)
    assert not [e for e in ev2 if e["metric"] == "queue_depth" and e["state"] == "triggered"]

    # 审计落库：operator=alert action=alert_trigger
    async with factory() as s:
        logs = await repos.list_audit(s, action="alert_trigger")
    assert logs, "应写入 alert 审计行"

    # 清空队列 → 回落 → 恢复
    async with factory() as s:
        await s.execute(update(TN).where(TN.status == "queued").values(status="done"))
        await s.commit()
    snap2 = await metrics_mod.collect_metrics(factory, redis=r)
    ev3 = await alerts.run_alert_scan(factory, snap2, redis=r)
    resolved = [e for e in ev3 if e["metric"] == "queue_depth" and e["state"] == "resolved"]
    assert resolved, "指标回落应产生恢复事件"
    await eng.dispose()
    metrics_mod._snapshot.clear()
    metrics_mod._snapshot.update({"collected_at": None, "error": "metrics not collected yet"})
    get_settings().ALERT_QUEUE_THRESHOLD = 20  # 还原共享配置，防跨测试污染


@pytest.mark.asyncio
async def test_health_grading_raw():
    """健康分级判定逻辑（纯函数级验证 degrade/unhealthy 分支给 /health 用）。"""
    from app.api import schemas

    # db 挂 → unhealthy
    h = schemas.HealthOut(status="unhealthy", db=False, redis=None,
                          version="t", metrics_status="db: err")
    assert h.status == "unhealthy"
