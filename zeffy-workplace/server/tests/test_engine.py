"""P1-2 工作流引擎测试：start/advance 全流程、顺序约束、乐观锁冲突。"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401
from app.db.init_db import init_db
from app.db.repos import create_task, get_node, list_nodes, set_node_status
from app.workflow import engine
from app.workflow.state_machine import WorkflowStateError


@pytest.fixture
async def session():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as s:
        yield s
    await eng.dispose()


async def test_start_creates_nodes_and_activates_first(session):
    task = await create_task(session, title="t", workflow_id="lightweight")
    first = await engine.start(session, task)
    assert first.status == "running"
    assert first.node_name == "需求"  # lightweight 首节点
    nodes = await list_nodes(session, task.id)
    assert len(nodes) == 4  # 需求/拆解/执行/验收
    assert task.status == "running"


async def test_advance_walks_lightweight_to_completion(session):
    task = await create_task(session, title="t", workflow_id="lightweight")
    first = await engine.start(session, task)
    nxt = await engine.advance(session, task.id, first.id, {"summary": "s1"})
    assert nxt is not None and nxt.node_name == "拆解"

    nxt2 = await engine.advance(session, task.id, nxt.id, {"plan": "p"})
    assert nxt2 is not None and nxt2.node_name == "执行"

    nxt3 = await engine.advance(session, task.id, nxt2.id, {"output": "o"})
    assert nxt3 is not None and nxt3.node_name == "验收"

    last = await engine.advance(session, task.id, nxt3.id, {"accepted": True})
    assert last is None  # 全部完成

    all_done = all(n.status == "done" for n in await list_nodes(session, task.id))
    assert all_done
    assert task.status == "done"


async def test_advance_unknown_node_raises(session):
    task = await create_task(session, title="t", workflow_id="lightweight")
    await engine.start(session, task)
    with pytest.raises(WorkflowStateError):
        await engine.advance(session, task.id, "no-such-id", None)


async def test_advance_out_of_order_blocked(session):
    task = await create_task(session, title="t", workflow_id="lightweight")
    await engine.start(session, task)
    nodes = await list_nodes(session, task.id)
    second = next(n for n in nodes if n.node_name == "拆解")
    # 顺序约束：前置节点未 done，禁止越序推进拆解
    with pytest.raises(WorkflowStateError) as ei:
        await engine.advance(session, task.id, second.id, None)
    assert "前置节点未完成" in str(ei.value)


async def test_advance_replayed_node_raises(session):
    task = await create_task(session, title="t", workflow_id="lightweight")
    first = await engine.start(session, task)
    await engine.advance(session, task.id, first.id, None)  # first -> done
    with pytest.raises(WorkflowStateError):
        await engine.advance(session, task.id, first.id, None)  # 已 done 不得再推进


async def test_optimistic_lock_returns_false_on_status_mismatch(session):
    """乐观锁：期望旧态不匹配时返回 False（并发防护 is_hit 规则）。"""
    task = await create_task(session, title="t", workflow_id="lightweight")
    first = await engine.start(session, task)
    node = await get_node(session, first.id)
    assert node is not None
    # 期望 pending，但实际 running → 不命中
    assert not await set_node_status(session, first.id, "pending", "done")
    # 期望 running → 命中
    assert await set_node_status(session, first.id, "running", "done")
