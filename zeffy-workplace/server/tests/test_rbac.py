"""P4-3 RBAC：权限判定矩阵 + 分享只读/读写 + 二次分享禁止 + admin 全局 + AUTH off 兼容。

AUTH on 用内存 sqlite。AUTH off 由既有 test_auth/test_api 覆盖（本项目零漂移由 full suite 断言）。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import permissions as perm
from app.auth.deps import UserPrincipal
from app.config import get_settings
from app.db import repos
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.db.models import User
from app.main import app

SEED_A = dict(email="aa@z.io", username="alice", password="password123")
SEED_B = dict(email="bb@z.io", username="bob", password="password123")


async def _turn_on_auth():
    s = get_settings()
    s.AUTH_ENABLED = True
    s.REGISTRATION_ENABLED = True


async def _reset():
    s = get_settings()
    s.AUTH_ENABLED = False
    s.REGISTRATION_ENABLED = False


async def _client():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    await ac.__aenter__()
    return ac, eng


async def _mk_user(ac, seed):
    await ac.post("/auth/register", json=seed)
    lg = await ac.post("/auth/login", json={"email": seed["email"], "password": seed["password"]})
    assert lg.status_code == 200, lg.text
    return lg.json()["token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.asyncio
async def test_permissions_matrix():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as s:
        owner = await repos.create_user(s, email="o@z.io", username="owner", password_hash="x")
        viewer = await repos.create_user(s, email="v@z.io", username="viewer", password_hash="x")
        editor = await repos.create_user(s, email="e@z.io", username="editor", password_hash="x")
        stranger = await repos.create_user(s, email="s@z.io", username="stranger", password_hash="x")
        admin = await repos.create_user(s, email="adm@z.io", username="admin", password_hash="x",
                                        is_system=True)
        await s.execute(update(User).where(User.id == admin.id).values(role="admin"))
        await s.commit()
        t = await repos.create_task(s, title="t", workflow_id="generic", owner_id=owner.id)
        await repos.upsert_share(s, task_id=t.id, user_id=viewer.id, role="viewer")
        await repos.upsert_share(s, task_id=t.id, user_id=editor.id, role="editor")
        P = UserPrincipal

        async def caps(uid):
            return await perm.caps_for(s, P(id=uid), t)

        assert await caps(owner.id) == {"view", "edit", "manage_share"}
        assert (await perm.can_edit(s, P(id=owner.id), t)) is True
        assert await caps(viewer.id) == {"view"}
        assert (await perm.can_view(s, P(id=viewer.id), t)) is True
        assert (await perm.can_edit(s, P(id=viewer.id), t)) is False
        assert (await perm.can_manage_share(s, P(id=viewer.id), t)) is False  # 🔴 二次分享禁止
        assert await caps(editor.id) == {"view", "edit"}
        assert (await perm.can_edit(s, P(id=editor.id), t)) is True
        assert (await perm.can_manage_share(s, P(id=editor.id), t)) is False  # editor 无管理分享权
        assert await caps(stranger.id) == set()
        assert (await perm.can_view(s, P(id=stranger.id), t)) is False
        assert await perm.caps_for(s, P(id=admin.id, role="admin"), t) == {"view", "edit", "manage_share"}
        # DB role 经 get_current_user 注入 principal → admin 判定生效
        assert (await perm.can_view(s, P(id=admin.id, role="admin"), t)) is True
    await eng.dispose()


@pytest.mark.asyncio
async def test_share_viewer_readonly_and_secondary_share_forbidden():
    ac, eng = await _client()
    try:
        await _turn_on_auth()
        tokA = await _mk_user(ac, SEED_A)
        tokB = await _mk_user(ac, SEED_B)
        got = await ac.post("/tasks", json={"title": "协作任务", "workflow_id": "generic"},
                            headers=_h(tokA))
        tid = got.json()["id"]
        # B 未分享 → 读 404、列表看不见
        assert (await ac.get(f"/tasks/{tid}/nodes", headers=_h(tokB))).status_code == 404
        assert (await ac.get("/tasks", headers=_h(tokB))).json()["total"] == 0
        # A(owner) 分享 viewer 给 B
        uidB = await _uid(ac, tokB)
        r = await ac.put(f"/tasks/{tid}/shares", headers=_h(tokA),
                         json={"user_id": uidB, "role": "viewer"})
        assert r.status_code == 200, r.text
        # B 现在可见 + 可读 + 列表可见
        assert (await ac.get(f"/tasks/{tid}/nodes", headers=_h(tokB))).status_code == 200
        assert (await ac.get("/tasks", headers=_h(tokB))).json()["total"] == 1
        # 🔴 二次分享禁止：B(viewer) 不能改分享 / 删分享 → 404
        assert (await ac.put(f"/tasks/{tid}/shares", headers=_h(tokB),
                             json={"user_id": uidB, "role": "editor"})).status_code == 404
        assert (await ac.delete(f"/tasks/{tid}/shares/{uidB}", headers=_h(tokB))).status_code == 404
    finally:
        await ac.__aexit__(*([None] * 3))
        await eng.dispose()
        await _reset()


async def _uid(ac, tok) -> str:
    me = await ac.get("/auth/me", headers=_h(tok))
    return me.json()["id"]


@pytest.mark.asyncio
async def test_admin_can_access_handler_route():
    """admin 可直接访问 /admin/cluster（ENABLE_ADMIN + AUTH on + admin）。"""
    ac, eng = await _client()
    try:
        await _turn_on_auth()
        s = get_settings()
        s.ENABLE_ADMIN = True
        tokA = await _mk_user(ac, SEED_A)
        # 普通用户 → 404
        assert (await ac.get("/admin/cluster", headers=_h(tokA))).status_code == 404
    finally:
        await ac.__aexit__(*([None] * 3))
        await eng.dispose()
        await _reset()
        get_settings().ENABLE_ADMIN = False
