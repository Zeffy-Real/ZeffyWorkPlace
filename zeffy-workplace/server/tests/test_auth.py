"""P3-3 鉴权：注册/登录/登出/me + owner 越权隔离 + AUTH off 兼容。

用内存 SQLite + FastAPI ASGI。改 ``get_settings().AUTH_ENABLED`` 做 on/off 两态断言。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app

SEED = dict(email="a@z.io", username="alice", password="password123")


async def _turn_on_auth():
    s = get_settings()
    s.AUTH_ENABLED = True
    s.REGISTRATION_ENABLED = True


async def _reset_auth():
    s = get_settings()
    s.AUTH_ENABLED = False
    s.REGISTRATION_ENABLED = False


async def _register(client) -> dict:
    r = await client.post("/auth/register", json=SEED)
    assert r.status_code == 200, r.text
    return r.json()


async def _login(client, email=SEED["email"], password=SEED["password"]) -> dict:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_register_login_me_flow():
    async with _auto_db() as client:
        await _turn_on_auth()
        await _register(client)
        tok = await _login(client)
        assert tok["token"].startswith("zwt_")
        me = await client.get("/auth/me", headers=_auth(tok["token"]))
        assert me.status_code == 200 and me.json()["email"] == SEED["email"]
        # 无 token → 401
        assert (await client.get("/auth/me")).status_code == 401
        out = await client.post("/auth/logout", headers=_auth(tok["token"]))
        assert out.status_code == 200
        # 登出后 token 失效
        assert (await client.get("/auth/me", headers=_auth(tok["token"]))).status_code == 401
        await _reset_auth()


@pytest.mark.asyncio
async def test_owner_isolation():
    """🔴 越权：A 建 A 任务，B 删不掉/读不到 A 任务；A 只能看到自己的任务。"""
    async with _auto_db() as ac:
        client = ac
        await _turn_on_auth()
        await _register(client)
        ta = await _login(client)
        tokA = ta["token"]

        new_user = {**SEED, "email": "b@z.io", "username": "bob"}
        await client.post("/auth/register", json=new_user)
        tb = await _login(client, email="b@z.io")
        tokB = tb["token"]

        # A 建任务
        got = await client.post("/tasks", json={"title": "A的任务", "workflow_id": "generic"},
                                headers=_auth(tokA))
        assert got.status_code == 200
        tid = got.json()["id"]

        # A list 可见自己的任务
        lst = await client.get("/tasks", headers=_auth(tokA))
        assert lst.status_code == 200 and lst.json()["total"] == 1

        # B 看不到 A 的任务（list 为空）
        lstb = await client.get("/tasks", headers=_auth(tokB))
        assert lstb.status_code == 200 and lstb.json()["total"] == 0

        # B 读 A 任务节点 → 403/404
        nodes = await client.get(f"/tasks/{tid}/nodes", headers=_auth(tokB))
        assert nodes.status_code in (403, 404)

        # A 自己读正常
        nodesa = await client.get(f"/tasks/{tid}/nodes", headers=_auth(tokA))
        assert nodesa.status_code == 200
        await _reset_auth()


@pytest.mark.asyncio
async def test_auth_off_compat():
    """AUTH off：无 token 也能读全部任务（P2 行为）。"""
    async with _auto_db() as client:
        await _reset_auth()
        r = await client.post("/tasks", json={"title": "无需登录", "workflow_id": "generic"})
        assert r.status_code == 200
        lst = await client.get("/tasks")
        assert lst.status_code == 200 and lst.json()["total"] == 1


@pytest.mark.asyncio
async def test_ws_token_validation():
    """🔴 WS 鉴权：错误 token→None（拒绝）；有效 token→Principal（放行）。"""
    from app.api.ws import _validate_token
    async with _auto_db():
        # 无效：非 zwt_ / 未知 token
        assert await _validate_token("zwt_deadbeef" * 8) is None
        assert await _validate_token("plain-text") is None
        # 注册+登录 → 有效
        s = get_settings()
        s.AUTH_ENABLED = True
        s.REGISTRATION_ENABLED = True
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await _register(c)
            tok = await _login(c)
            p = await _validate_token(tok["token"])
        assert p is not None and p.authenticated and p.id
        await _reset_auth()


class _auto_db:
    """上下文管理器：初始化内存 engine 并注入全局。"""

    async def __aenter__(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await init_db(self.engine)
        set_global_engine(self.engine)
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        await ac.__aenter__()
        self.ac = ac
        return self.ac

    async def __aexit__(self, *exc):
        await self.ac.__aexit__(*exc)
        await self.engine.dispose()
        await _reset_auth()
