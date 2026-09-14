"""P2 队列：worker job 端到端（认领→AgentRunner→事件发布）+ 认领幂等（乐观锁）。

不依赖真 ARQ 轮询：直接调用 job 函数 + 注入内存 DB/假 publish/假 LLM（等价 job 执行体）。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401
from app.db.init_db import init_db
from app.db.repos import claim_node, create_task, list_nodes, set_node_queued
from app.queue.arqs import run_agent_task
from app.workflow import engine
from tests.conftest import FakeLLM, plan_reply


class FakePub:
    """假 publish Redis：记录发布的事件（含 seq 递增）。"""

    def __init__(self):
        self.events = []

    async def publish(self, channel, data):
        self.events.append((channel, data))


async def _factory_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    return async_sessionmaker(eng, expire_on_commit=False), eng


def _patched_llm(monkeypatch, replies):
    fake = FakeLLM(replies)
    monkeypatch.setattr("app.agents.base.get_llm", lambda *a, **k: fake)
    return fake


async def test_claim_idempotent():
    factory, eng = await _factory_engine()
    async with factory() as s:
        t = await create_task(s, title="x", workflow_id="generic")
        from app.db.repos import create_node
        n = await create_node(s, task_id=t.id, node_name="A")
        await set_node_queued(s, n.id)
        await s.commit()
        first = await claim_node(s, n.id, worker_id="w1",
                                 lease_expire_at=datetime.now(UTC) + timedelta(seconds=60))
        assert first is True
        await s.commit()
        second = await claim_node(s, n.id, worker_id="w2",
                                  lease_expire_at=datetime.now(UTC) + timedelta(seconds=60))
        assert second is False  # 已 running，第二个 worker 认领失败
    await eng.dispose()


async def test_worker_job_runs_lightweight_to_interrupt(monkeypatch):
    factory, eng = await _factory_engine()
    async with factory() as s:
        task = await create_task(s, title="做件事", description="简略需求", workflow_id="lightweight")
        await engine.prepare(s, task)
        nodes = await list_nodes(s, task.id)
        await set_node_queued(s, nodes[0].id)
        await s.commit()

    _patched_llm(monkeypatch, [
        plan_reply("做一个工具"),  # 需求(supervisor)
        ("拆解明细", {"prompt_tokens": 1, "completion_tokens": 1}),  # 拆解(planner)
        ("执行产物：工具实现", {"prompt_tokens": 1, "completion_tokens": 1}),  # 执行(doer)
    ])

    pub = FakePub()
    ctx = {"session_factory": factory, "registry": None, "publish_redis": pub}
    outcome = await run_agent_task(ctx, task.id)

    assert outcome is not None
    assert outcome["status"] == "interrupt"  # 走到验收 HITL
    # 事件已发布（agent_message / task_node_update）
    assert pub.events, "worker 应发布事件"
    kinds = {json_load_kind(d) for _, d in pub.events}
    assert "task_node_update" in kinds
    await eng.dispose()


def json_load_kind(data: bytes) -> str:
    import json
    return json.loads(data)["kind"]


async def test_worker_job_crash_sets_failed(monkeypatch):
    """🔴 审查：job 例外不击穿，节点置 failed。"""
    from app.llm_errors import LLMConfigError

    factory, eng = await _factory_engine()
    async with factory() as s:
        task = await create_task(s, title="t", workflow_id="lightweight")
        await engine.prepare(s, task)
        nodes = await list_nodes(s, task.id)
        await set_node_queued(s, nodes[0].id)
        await s.commit()

    _patched_llm(monkeypatch, [LLMConfigError("no key")])
    pub = FakePub()
    ctx = {"session_factory": factory, "registry": None, "publish_redis": pub}
    outcome = await run_agent_task(ctx, task.id)

    assert outcome["status"] == "error"
    async with factory() as s:
        from app.db.repos import get_task
        t = await get_task(s, task.id)
        assert t.status == "failed"  # 异常→任务 failed，不击穿
    await eng.dispose()
