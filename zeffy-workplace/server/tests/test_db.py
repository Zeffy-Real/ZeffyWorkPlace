"""P0-3 数据层测试：内存 SQLite 建表/写读 + repo 基础接口。"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401  确保注册进 metadata
from app.db.init_db import init_db
from app.db.repos import create_task, get_task, list_tasks


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_and_get_task(db_session):
    task = await create_task(db_session, title="写 PRD", description="某需求")
    assert task.id
    assert task.status == "pending"
    got = await get_task(db_session, task.id)
    assert got is not None
    assert got.title == "写 PRD"


@pytest.mark.asyncio
async def test_list_tasks(db_session):
    await create_task(db_session, title="A")
    await create_task(db_session, title="B", workflow_id="lightweight")
    all_tasks = await list_tasks(db_session)
    assert len(all_tasks) == 2


@pytest.mark.asyncio
async def test_message_and_node_write(db_session):
    """Message 与 TaskNode 可写入且外键关联正确。"""
    from app.db.models import Message, TaskNode

    task = await create_task(db_session, title="X")
    m = Message(task_id=task.id, sender_role="user", content="hello")
    n = TaskNode(task_id=task.id, node_name="plan", status="done")
    db_session.add_all([m, n])
    await db_session.commit()

    msgs = (await db_session.execute(select(Message))).scalars().all()
    nodes = (await db_session.execute(select(TaskNode))).scalars().all()
    assert len(msgs) == 1
    assert len(nodes) == 1
    assert msgs[0].task_id == task.id


@pytest.mark.asyncio
async def test_init_db_creates_expected_tables(db_session):
    from sqlalchemy import inspect

    def _names(conn):
        return set(inspect(conn).get_table_names())

    conn = await db_session.connection()
    try:
        names = await conn.run_sync(_names)
    finally:
        await conn.close()
    assert {"tasks", "messages", "task_nodes", "audit_logs", "eval_runs"} <= names
