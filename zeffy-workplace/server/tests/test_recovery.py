"""P2 断点恢复白名单 + lease 巡检测试（🔴 审查核心场景）。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401
from app.db.init_db import init_db
from app.db.repos import (
    claim_node,
    create_node,
    create_task,
    list_nodes,
    set_node_queued,
    set_task_status,
)
from app.queue.recovery import _lease_stale, resume_inflight


async def _factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    return async_sessionmaker(eng, expire_on_commit=False), eng


async def _mk_from_state(factory, task_status="pending", first_node="queued", lease=None):
    """建任务 + 首节点状态；return task_id."""
    async with factory() as s:
        t = await create_task(s, title="x", workflow_id="generic")
        for name in ["需求分析", "文档", "设计", "实现", "评审", "验收"]:
            await create_node(s, task_id=t.id, node_name=name)
        nodes = await list_nodes(s, t.id)
        n0 = nodes[0]
        if first_node == "queued":
            await set_node_queued(s, n0.id)
            await s.commit()
        elif first_node == "running":
            await set_node_queued(s, n0.id)
            await claim_node(s, n0.id, worker_id=lease.get("worker_id", "w"),
                             lease_expire_at=lease.get("expire", datetime.now(UTC) + timedelta(600)))
            await s.commit()
        if task_status != "pending":
            await set_task_status(s, t.id, task_status)
            await s.commit()
        return t.id


async def _run(factory):
    captured = []

    async def enqueue(tid):
        captured.append(tid)

    stats = await resume_inflight(factory, enqueue)
    return stats, captured


async def test_re_enqueue_queued():
    factory, eng = await _factory()
    tid = await _mk_from_state(factory, first_node="queued")
    stats, captured = await _run(factory)
    assert stats["queued"] == 1 and stats["re_enqueued"] == 1
    assert captured == [tid]
    await eng.dispose()


async def test_skip_done_task():
    factory, eng = await _factory()
    await _mk_from_state(factory, task_status="done", first_node="queued")
    stats, captured = await _run(factory)
    assert stats["ignored"] == 1 and captured == []
    await eng.dispose()


async def test_recover_expired_lease_running():
    factory, eng = await _factory()
    expired = datetime.now(UTC) - timedelta(seconds=10)
    await _mk_from_state(factory, first_node="running",
                         lease={"worker_id": "dead", "expire": expired})
    stats, captured = await _run(factory)
    assert stats["recover"] == 1 and stats["re_enqueued"] == 1
    await eng.dispose()


async def test_skip_active_lease_running():
    factory, eng = await _factory()
    alive = datetime.now(UTC) + timedelta(seconds=600)
    await _mk_from_state(factory, first_node="running",
                         lease={"worker_id": "alive", "expire": alive})
    stats, captured = await _run(factory)
    assert stats["active_skip"] == 1 and stats["re_enqueued"] == 0
    await eng.dispose()


def test_lease_stale_util():
    assert _lease_stale(None) is False  # 活跃/未知：不回收，ARQ 兜底
    assert _lease_stale({"expire_at": "2020-01-01T00:00:00+00:00"}) is True  # 显式过期
    assert _lease_stale({"expire_at": "2999-01-01T00:00:00+00:00"}) is False


async def test_skip_running_without_lease():
    """running 节点无 lease（任务级 job 的后续节点活跃执行中）→ 不回收。"""
    factory, eng = await _factory()
    # 先造一个：running 带活跃 lease（active_skip）
    await _mk_from_state(factory, first_node="running",
                         lease={"worker_id": "w", "expire": datetime.now(UTC) + timedelta(600)})
    # 再造一个 running 但 lease=None（活跃增长中的后续节点）
    async with factory() as s:
        t = await create_task(s, title="y", workflow_id="generic")
        for name in ["需求分析", "文档", "设计", "实现", "评审", "验收"]:
            await create_node(s, task_id=t.id, node_name=name)
        nodes = await list_nodes(s, t.id)
        await set_node_queued(s, nodes[0].id)
        await claim_node(s, nodes[0].id, worker_id="w",
                         lease_expire_at=datetime.now(UTC) + timedelta(600))
        from sqlalchemy import update
        await s.execute(update(type(nodes[0])).where(type(nodes[0]).id == nodes[0].id).values(lease=None))
        await s.commit()
    stats, captured = await _run(factory)
    assert stats["active_skip"] >= 1 and captured == []  # 两者都不回收
    await eng.dispose()
