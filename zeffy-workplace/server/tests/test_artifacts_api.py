"""P5 /artifacts API：读/列/删鉴权闭环 + 越权 404 + AUTH off 放行 + 路径逃逸。

- AUTH off：匿名全放行（P2 兼容）；写产物→列表→读取→删除→404。
- AUTH on：owner 可读/删；陌生用户越权一律 404（防枚举）。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import repos
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app
from app.storage import reset_backend, set_backend
from app.storage.local import LocalBackend


@pytest.fixture
async def client_env(tmp_path):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    backend = LocalBackend(tmp_path)
    set_backend(backend)
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac, backend
    await ac.__aexit__(None, None, None)
    await eng.dispose()
    reset_backend()
    # 恢复 AUTH 默认关闭（兼容锚点）
    s = get_settings()
    s.AUTH_ENABLED = False
    s.REGISTRATION_ENABLED = False


async def _mk_task():
    from app.db.base import get_session_factory

    factory = get_session_factory()
    async with factory() as s:
        return await repos.create_task(s, title="t", workflow_id="generic")


@pytest.mark.asyncio
async def test_artifacts_roundtrip_auth_off(client_env):
    ac, backend = client_env
    task = await _mk_task()
    await backend.put(f"artifacts/{task.id}/doc.md", b"# Hello", mode="no_overwrite")

    # 列表
    r = await ac.get(f"/artifacts/{task.id}")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert f"artifacts/{task.id}/doc.md" in r.json()["keys"]

    # 读取（流）
    r = await ac.get(f"/artifacts/{task.id}/doc.md")
    assert r.status_code == 200, r.text
    assert r.content == b"# Hello"

    # 删除（写 → can_edit；AUTH off 放行）
    r = await ac.delete(f"/artifacts/{task.id}/doc.md")
    assert r.status_code == 200, r.text
    # 再读 404
    r = await ac.get(f"/artifacts/{task.id}/doc.md")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_artifacts_path_escape_404(client_env):
    ac, backend = client_env
    task = await _mk_task()
    await backend.put(f"artifacts/{task.id}/doc.md", b"x", mode="no_overwrite")
    for bad in ("../etc/passwd", "/etc/passwd", "..%2Fpasswd"):
        r = await ac.get(f"/artifacts/{task.id}/{bad}")
        assert r.status_code == 404, (bad, r.status_code)


@pytest.mark.asyncio
async def test_artifacts_unauthorized_404(client_env):
    """AUTH on：陌生用户越权访问一律 404。"""
    s = get_settings()
    s.AUTH_ENABLED = True
    s.REGISTRATION_ENABLED = True
    ac, backend = client_env

    await ac.post("/auth/register", json={"email": "a@z.io", "username": "alice",
                                          "password": "password123"})
    await ac.post("/auth/register", json={"email": "b@z.io", "username": "bob",
                                          "password": "password123"})
    tok_a = (await ac.post("/auth/login", json={"email": "a@z.io", "password": "password123"})).json()["token"]
    tok_b = (await ac.post("/auth/login", json={"email": "b@z.io", "password": "password123"})).json()["token"]

    # alice 建任务
    t = (await ac.post("/tasks", json={"title": "t", "workflow_id": "generic"},
                       headers={"Authorization": f"Bearer {tok_a}"})).json()
    tid = t["id"]
    await backend.put(f"artifacts/{tid}/secret.md", b"top secret", mode="no_overwrite")

    # 陌生用户 bob：读/列/删一律 404
    assert (await ac.get(f"/artifacts/{tid}/secret.md",
                         headers={"Authorization": f"Bearer {tok_b}"})).status_code == 404
    assert (await ac.get(f"/artifacts/{tid}",
                         headers={"Authorization": f"Bearer {tok_b}"})).status_code == 404
    assert (await ac.delete(f"/artifacts/{tid}/secret.md",
                            headers={"Authorization": f"Bearer {tok_b}"})).status_code == 404

    # owner alice：可读
    r = await ac.get(f"/artifacts/{tid}/secret.md",
                     headers={"Authorization": f"Bearer {tok_a}"})
    assert r.status_code == 200 and r.content == b"top secret"
