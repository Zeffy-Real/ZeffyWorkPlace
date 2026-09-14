"""P3 恢复白名单修订测试：
- queued 保留在 ARQ，不重复入队（queued_skip）；
- 只回收 running 死任务（lease 过期 / 无 lease 且超时）；
- 死信 attempts 超限；分区扫描锁（Redis SET NX EX）仅单实例执行。
"""

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import models  # noqa: F401
from app.db.init_db import init_db
from app.db.models import TaskNode
from app.db.repos import (
    claim_node,
    create_node,
    create_task,
    get_task,
    list_nodes,
    set_node_queued,
    set_task_status,
)
from app.queue.recovery import SCAN_LOCK_KEY, _running_stale, resume_inflight

NAMES = ["需求分析", "文档", "设计", "实现", "评审", "验收"]


async def _factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    return async_sessionmaker(eng, expire_on_commit=False), eng


async def _mk(factory, *, task_status="pending", first="none", lease=None,
              attempts=None, updated_at=None):
    async with factory() as s:
        t = await create_task(s, title="x", workflow_id="generic")
        for name in NAMES:
            await create_node(s, task_id=t.id, node_name=name)
        nodes = await list_nodes(s, t.id)
        n0 = nodes[0]
        if first == "queued":
            await set_node_queued(s, n0.id)
        elif first == "running":
            await set_node_queued(s, n0.id)
            await claim_node(s, n0.id,
                             worker_id=(lease or {}).get("worker_id", "w") if lease else "w",
                             lease_expire_at=(lease or {}).get(
                                 "expire", datetime.now(UTC) + timedelta(600)))
        if attempts is not None:
            await s.execute(update(TaskNode).where(TaskNode.id == n0.id)
                            .values(attempts=attempts))
        if updated_at is not None:
            await s.execute(update(TaskNode).where(TaskNode.id == n0.id)
                            .values(updated_at=updated_at,
                                    **({"lease": None} if lease is None and first == "running" else {})))
        elif lease is None and first == "running":
            await s.execute(update(TaskNode).where(TaskNode.id == n0.id).values(lease=None))
        await s.commit()
        if task_status != "pending":
            await set_task_status(s, t.id, task_status)
            await s.commit()
        return t.id


async def _run(factory, redis=None):
    captured = []

    async def enqueue(tid):
        captured.append(tid)

    stats = await resume_inflight(factory, enqueue, redis=redis)
    return stats, captured


# ---- queued 不重入（P3 修订） ----

async def test_queued_never_re_enqueued():
    """queued 节点保留在 ARQ，不重复入队（stale 与否都 skip）。"""
    factory, eng = await _factory()
    await _mk(factory, first="queued")
    stats, captured = await _run(factory)
    assert stats["queued_skip"] == 1 and captured == []
    await eng.dispose()


# ---- running 死任务回收 ----

async def test_running_expired_lease_recovered(monkeypatch):
    factory, eng = await _factory()
    expired = datetime.now(UTC) - timedelta(seconds=10)
    # 设 lease TTL 极小，避免 updated_at 干扰
    monkeypatch.setattr(get_settings(), "LEASE_TTL", 1)
    tid = await _mk(factory, first="running",
                    lease={"worker_id": "dead", "expire": expired})
    stats, captured = await _run(factory)
    assert stats["recover"] == 1 and stats["re_enqueued"] == 1 and captured == [tid]
    await eng.dispose()


async def test_running_active_lease_skipped():
    factory, eng = await _factory()
    alive = datetime.now(UTC) + timedelta(seconds=600)
    await _mk(factory, first="running", lease={"worker_id": "alive", "expire": alive})
    stats, captured = await _run(factory)
    assert stats["active_skip"] == 1 and stats["re_enqueued"] == 0 and captured == []
    await eng.dispose()


async def test_running_without_lease_fresh_skipped():
    """无 lease 但 updated_at 新（活跃执行中）→ 不回收。"""
    factory, eng = await _factory()
    await _mk(factory, first="running", lease=None)
    stats, captured = await _run(factory)
    assert stats["active_skip"] >= 1 and captured == []
    await eng.dispose()


async def test_running_without_lease_stale_recovered(monkeypatch):
    """无 lease 且 updated_at 超 grace（worker 永久死亡）→ 回收。"""
    factory, eng = await _factory()
    monkeypatch.setattr(get_settings(), "LEASE_TTL", 1)
    old = datetime.now(UTC) - timedelta(seconds=600)
    tid = await _mk(factory, first="running", lease=None, updated_at=old)
    stats, captured = await _run(factory)
    assert stats["recover"] == 1 and stats["re_enqueued"] == 1 and captured == [tid]
    await eng.dispose()


# ---- 死信（attempts 超限，仅 running） ----

async def test_running_dead_letter_attempts_exceeded(monkeypatch):
    from sqlalchemy import select

    from app.db.models import AuditLog

    factory, eng = await _factory()
    monkeypatch.setattr(get_settings(), "LEASE_TTL", 1)
    expired = datetime.now(UTC) - timedelta(seconds=10)
    tid = await _mk(factory, first="running",
                    lease={"worker_id": "dead", "expire": expired},
                    attempts=get_settings().ARQ_MAX_TRIES)
    stats, captured = await _run(factory)
    assert stats["dead_letter"] == 1 and captured == []
    async with factory() as s:
        task = await get_task(s, tid)
        nodes = {n.node_name: n for n in await list_nodes(s, tid)}
        audits = list((await s.execute(select(AuditLog).where(AuditLog.task_id == tid))).scalars())
    assert nodes["需求分析"].status == "failed" and task.status == "failed"
    assert any(a.action == "dead_letter" for a in audits)
    await eng.dispose()


# ---- 白名单 ----

async def test_skip_done_task():
    factory, eng = await _factory()
    await _mk(factory, task_status="done", first="running")
    stats, captured = await _run(factory)
    assert stats["ignored"] == 1 and captured == []
    await eng.dispose()


# ---- 分区扫描锁 ----

async def test_scan_lock_locked_out():
    """已持锁 → 不扫描。"""
    factory, eng = await _factory()
    await _mk(factory, first="running",
              lease={"worker_id": "dead", "expire": datetime.now(UTC) - timedelta(10)})
    r = fakeredis.aioredis.FakeRedis()
    await r.set(SCAN_LOCK_KEY, "other-worker", nx=True, ex=100)
    stats, captured = await _run(factory, redis=r)
    assert stats["locked_out"] == 1 and captured == []
    await eng.dispose()


async def test_scan_lock_acquired_runs():
    """未持锁 → 抢到锁并扫描回收 running 死任务。"""
    factory, eng = await _factory()

    prev = get_settings().LEASE_TTL
    get_settings().LEASE_TTL = 1
    try:
        tid = await _mk(factory, first="running",
                        lease={"worker_id": "dead", "expire": datetime.now(UTC) - timedelta(10)})
        r = fakeredis.aioredis.FakeRedis()
        stats, captured = await _run(factory, redis=r)
        assert stats["recover"] == 1 and stats["re_enqueued"] == 1 and captured == [tid]
    finally:
        get_settings().LEASE_TTL = prev
    await eng.dispose()


# ---- 工具 ----

def test_running_stale_util(monkeypatch):
    monkeypatch.setattr(get_settings(), "LEASE_TTL", 30)
    old = datetime.now(UTC) - timedelta(seconds=100)

    class N:
        lease = None
        updated_at = old
        created_at = old

    assert _running_stale(N()) is True

    class Fresh(N):
        updated_at = datetime.now(UTC)
        created_at = datetime.now(UTC)

    assert _running_stale(Fresh()) is False
    assert _running_stale(type("NL", (), {"lease": None, "updated_at": None,
                                          "created_at": None})()) is True
