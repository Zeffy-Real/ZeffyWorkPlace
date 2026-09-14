"""P5-1 /artifacts 版本接口：?version=N 读取 / _versions 列表 / _diff / 单版本删除 + 鉴权 404。

- 版本开启（set_backend(VersionManager)）：读/列/diff/删版本全通。
- AUTH on：陌生用户越权一律 404。
- 版本关闭：版本接口 404（兼容锚点）。
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
from app.storage.versioning import VersionManager


@pytest.fixture
async def v_api_env(tmp_path):
    """版本开启的 API 环境（全局 engine + VersionManager 后端）。"""
    s = get_settings()
    s.ARTIFACT_VERSIONS_ENABLED = True
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    from app.db.base import get_session_factory

    vm = VersionManager(LocalBackend(tmp_path), session_factory=get_session_factory())
    set_backend(vm)
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac, vm
    await ac.__aexit__(None, None, None)
    await eng.dispose()
    reset_backend()
    s.ARTIFACT_VERSIONS_ENABLED = False
    s.AUTH_ENABLED = False
    s.REGISTRATION_ENABLED = False


async def _mk_task():
    from app.db.base import get_session_factory

    factory = get_session_factory()
    async with factory() as s:
        return await repos.create_task(s, title="v", workflow_id="generic")


@pytest.mark.asyncio
async def test_version_api_roundtrip(v_api_env):
    ac, vm = v_api_env
    task = await _mk_task()
    rel = "doc.md"
    key = f"artifacts/{task.id}/{rel}"
    await vm.put(key, b"v1\nline2\n", mode="overwrite", run_id="r1")
    await vm.put(key, b"v1\nline2-new\n", mode="overwrite", run_id="r2")

    # 读历史版本
    r = await ac.get(f"/artifacts/{task.id}/{rel}?version=1")
    assert r.status_code == 200 and r.content == b"v1\nline2\n"
    assert r.headers.get("X-Version") == "1"
    r = await ac.get(f"/artifacts/{task.id}/{rel}?version=99")
    assert r.status_code == 404

    # 版本列表（分页 + 总数 + 总字节）
    r = await ac.get(f"/artifacts/{task.id}/_versions?path={rel}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["total_bytes"] == len(b"v1\nline2\n") + len(b"v2\nline2-new\n")
    assert [i["version"] for i in body["items"]] == [2, 1]

    # diff
    r = await ac.get(f"/artifacts/{task.id}/_diff?path={rel}&from_v=1&to_v=2")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok" and d["added"] == 1 and d["removed"] == 1

    # 单版本删除 → 再读 404；另一版本仍可读
    r = await ac.delete(f"/artifacts/{task.id}/{rel}?version=1")
    assert r.status_code == 200
    assert (await ac.get(f"/artifacts/{task.id}/{rel}?version=1")).status_code == 404
    assert (await ac.get(f"/artifacts/{task.id}/{rel}?version=2")).status_code == 200

    # 删主 key → 全部版本级联清
    r = await ac.delete(f"/artifacts/{task.id}/{rel}")
    assert r.status_code == 200
    r = await ac.get(f"/artifacts/{task.id}/_versions?path={rel}")
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_version_api_unauthorized_404(v_api_env):
    """AUTH on：陌生用户越权访问版本接口一律 404。"""
    s = get_settings()
    s.AUTH_ENABLED = True
    s.REGISTRATION_ENABLED = True
    ac, vm = v_api_env

    await ac.post("/auth/register", json={"email": "a@z.io", "username": "alice",
                                          "password": "password123"})
    await ac.post("/auth/register", json={"email": "b@z.io", "username": "bob",
                                          "password": "password123"})
    tok_a = (await ac.post("/auth/login", json={"email": "a@z.io", "password": "password123"})).json()["token"]
    tok_b = (await ac.post("/auth/login", json={"email": "b@z.io", "password": "password123"})).json()["token"]

    t = (await ac.post("/tasks", json={"title": "t", "workflow_id": "generic"},
                       headers={"Authorization": f"Bearer {tok_a}"})).json()
    tid = t["id"]
    await vm.put(f"artifacts/{tid}/doc.md", b"secret v1", mode="overwrite", run_id="r1")

    bh = {"Authorization": f"Bearer {tok_b}"}
    assert (await ac.get(f"/artifacts/{tid}/doc.md?version=1", headers=bh)).status_code == 404
    assert (await ac.get(f"/artifacts/{tid}/_versions?path=doc.md", headers=bh)).status_code == 404
    assert (await ac.get(f"/artifacts/{tid}/_diff?path=doc.md&from_v=1&to_v=1", headers=bh)).status_code == 404
    assert (await ac.delete(f"/artifacts/{tid}/doc.md?version=1", headers=bh)).status_code == 404

    # owner 可读
    ah = {"Authorization": f"Bearer {tok_a}"}
    assert (await ac.get(f"/artifacts/{tid}/doc.md?version=1", headers=ah)).status_code == 200


@pytest.mark.asyncio
async def test_version_api_disabled_404(v_api_env):
    """版本关闭：版本接口 404（兼容锚点），普通读取正常。"""
    s = get_settings()
    s.ARTIFACT_VERSIONS_ENABLED = False
    ac, vm = v_api_env
    task = await _mk_task()
    key = f"artifacts/{task.id}/doc.md"
    # 关闭后 backend 直通（VersionManager 实例仍存在但 enabled=False）
    await vm.put(key, b"plain", mode="overwrite")

    assert (await ac.get(f"/artifacts/{task.id}/doc.md?version=1")).status_code == 404
    assert (await ac.get(f"/artifacts/{task.id}/_versions?path=doc.md")).status_code == 404
    assert (await ac.get(f"/artifacts/{task.id}/doc.md")).status_code == 200
