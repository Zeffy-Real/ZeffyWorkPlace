"""P0-4 API + WebSocket 集成测试。

使用 FastAPI app 直接跑 ASGI（httpx ASGITransport），DB 用内存 SQLite，
替换全局 engine，避免依赖 Docker PG。注意：/health 的 db 检测依赖真实 connect，
本测试用 sqlite 注入后 health.db 应为 True。
"""


import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    set_global_engine(engine)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await engine.dispose()


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert "db" in body


@pytest.mark.asyncio
async def test_create_task(client):
    resp = await client.post(
        "/tasks",
        json={"title": "写 PRD", "description": "某需求", "workflow_id": "generic"},
    )
    assert resp.status_code == 200
    task = resp.json()
    assert task["title"] == "写 PRD"
    assert task["status"] == "pending"
    assert task["id"]


@pytest.mark.asyncio
async def test_list_tasks(client):
    await client.post("/tasks", json={"title": "A"})
    await client.post("/tasks", json={"title": "B", "workflow_id": "lightweight"})
    resp = await client.get("/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_create_task_invalid_empty_title(client):
    resp = await client.post("/tasks", json={"title": ""})
    assert resp.status_code == 422
