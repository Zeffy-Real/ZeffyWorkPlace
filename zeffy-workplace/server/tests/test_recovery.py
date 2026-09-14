"""P2 断点恢复白名单 + lease 巡检 + 一致性/死信测试（🔴 审查核心场景）。"""

from datetime import UTC, datetime, timedelta

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
    renew_node_lease,
    set_node_queued,
    set_task_status,
)
from app.queue.recovery import _lease_expired, _running_stale, resume_inflight

NAMES = ["需求分析", "文档", "设计", "实现", "评审", "验收"]


async def _factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    return async_sessionmaker(eng, expire_on_commit=False), eng


async def _mk(factory, *, task_status="pending", first="none", lease=None,
             stale_queued=False, attempts=None, updated_at=None):
    """建任务+6 节点；first=queued/running 配置首节点；可选置 stale/旧更新时间。"""
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
            await claim_node(s, n0.id, worker_id=lease.get("worker_id", "w") if lease else "w",
                             lease_expire_at=(lease or {}).get(
                                 "expire", datetime.now(UTC) + timedelta(600)))
        if stale_queued:
            old = datetime.now(UTC) - timedelta(seconds=get_settings().QUEUED_STALE_SECONDS * 2)
            await s.execute(update(TaskNode).where(TaskNode.id == n0.id)
                            .values(queued_at=old))
        if attempts is not None:
            await s.execute(update(TaskNode).where(TaskNode.id == n0.id)
                            .values(attempts=attempts))
        if updated_at is not None:
            # 显式提供 updated_at 时列级 onupdate 不会覆盖；与 lease=None 合并为一条语句
            await s.execute(update(TaskNode).where(TaskNode.id == n0.id)
                            .values(updated_at=updated_at,
                                    **({"lease": None} if lease is None and first == "running" else {})))
        elif lease is None and first == "running":
            # 无 lease 的 running（模拟任务级 job 后续节点）
            await s.execute(update(TaskNode).where(TaskNode.id == n0.id).values(lease=None))
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


# ---- 工具函数 ----

def test_lease_expired_util():
    assert _lease_expired(None) is False
    assert _lease_expired({"expire_at": "2020-01-01T00:00:00+00:00"}) is True
    assert _lease_expired({"expire_at": "2999-01-01T00:00:00+00:00"}) is False


def test_running_stale_util():
    old = datetime.now(UTC) - timedelta(seconds=get_settings().ARQ_JOB_TIMEOUT * 2)

    class N:  # 最小桩
        lease = None
        updated_at = old
        created_at = old

    assert _running_stale(N()) is True  # 无 lease + 旧 updated_at → 回收

    class Fresh(N):
        updated_at = datetime.now(UTC)
        created_at = datetime.now(UTC)

    assert _running_stale(Fresh()) is False  # 无 lease 但新 → 不回收


# ---- queued 一致性巡检 ----

async def test_queued_fresh_not_re_enqueued():
    """刚入队（未超 stale 窗口）→ 不重入队（避免与在途 job 竞争）。"""
    factory, eng = await _factory()
    await _mk(factory, first="queued", stale_queued=False)
    stats, captured = await _run(factory)
    assert stats["queued_fresh_skip"] == 1 and captured == []
    await eng.dispose()


async def test_queued_stale_re_enqueued():
    """queued 滞留超时（job 丢失）→ 重新入队（一致性兜底）。"""
    factory, eng = await _factory()
    tid = await _mk(factory, first="queued", stale_queued=True)
    stats, captured = await _run(factory)
    assert stats["queued"] == 1 and stats["re_enqueued"] == 1 and captured == [tid]
    await eng.dispose()


async def test_queued_dead_letter_when_attempts_exceeded():
    """attempts ≥ ARQ_MAX_TRIES 仍滞留 → 死信终止（节点/任务 failed + 审计）。"""
    from sqlalchemy import select

    from app.db.models import AuditLog

    factory, eng = await _factory()
    tid = await _mk(factory, first="queued", stale_queued=True,
                    attempts=get_settings().ARQ_MAX_TRIES)
    stats, captured = await _run(factory)
    assert stats["dead_letter"] == 1 and captured == []
    async with factory() as s:
        task = await get_task(s, tid)
        nodes = {n.node_name: n for n in await list_nodes(s, tid)}
        audits = list((await s.execute(select(AuditLog).where(AuditLog.task_id == tid))).scalars())
    assert nodes["需求分析"].status == "failed"
    assert task.status == "failed"
    assert any(a.action == "dead_letter" for a in audits)
    await eng.dispose()


# ---- running lease 巡检 ----

async def test_running_expired_lease_recovered():
    factory, eng = await _factory()
    expired = datetime.now(UTC) - timedelta(seconds=10)
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
    await _mk(factory, first="running", lease=None)  # claim 后清 lease
    stats, captured = await _run(factory)
    assert stats["active_skip"] >= 1 and captured == []
    await eng.dispose()


async def test_running_without_lease_stale_recovered():
    """无 lease 且 updated_at 超 grace（worker 永久死亡）→ 回收。"""
    factory, eng = await _factory()
    old = datetime.now(UTC) - timedelta(seconds=get_settings().ARQ_JOB_TIMEOUT * 2)
    tid = await _mk(factory, first="running", lease=None, updated_at=old)
    stats, captured = await _run(factory)
    assert stats["recover"] == 1 and stats["re_enqueued"] == 1 and captured == [tid]
    await eng.dispose()


# ---- 白名单 ----

async def test_skip_done_task():
    factory, eng = await _factory()
    await _mk(factory, task_status="done", first="queued", stale_queued=True)
    stats, captured = await _run(factory)
    assert stats["ignored"] == 1 and captured == []
    await eng.dispose()


# ---- lease 续约 repo ----

async def test_renew_lease_only_when_running():
    factory, eng = await _factory()
    tid = await _mk(factory, first="running")
    async with factory() as s:
        nodes = await list_nodes(s, tid)
        expire = datetime.now(UTC) + timedelta(seconds=600)
        assert await renew_node_lease(s, nodes[0].id, worker_id="w2",
                                      expire_at=expire) is True
        # 已 done 的节点不可续约
        assert await renew_node_lease(s, nodes[1].id, worker_id="w2",
                                      expire_at=expire) is False
    await eng.dispose()
