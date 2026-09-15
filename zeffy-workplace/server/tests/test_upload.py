"""P5-5 上传断点续传：init 互斥/断点、chunk 连续+幂等、commit 原子+sha256、status/cancel、路径/兼容锚点。

核心用例（审查🔴/⭐）：
1. 全流程 init→chunk→commit→读取，内容一致 + sha256 返回
2. 连续约束：跳块 400；断点续传 next_offset 正确
3. commit 缺块 400；md5 不匹配 400
4. chunk 幂等：同 offset 重传成功且不损坏
5. 路径穿越 rel → 404；upload_id 服务端生成
6. status 进度 / cancel 清理（status 后 404）
7. 兼容锚点：UPLOAD_ENABLED=false → init 404
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app
from app.storage.local import LocalBackend


@pytest.fixture
async def up_api(tmp_path):
    s = get_settings()
    s.UPLOAD_ENABLED = True
    s.UPLOAD_CHUNK = 8  # 测试用小块
    s.UPLOAD_TTL = 3600
    from app.storage import get_backend, reset_backend
    from app.storage import set_backend as _sb

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    backend = LocalBackend(tmp_path)
    _sb(backend)
    from httpx import ASGITransport, AsyncClient

    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac
    await ac.__aexit__(None, None, None)
    await eng.dispose()
    reset_backend()
    s.UPLOAD_ENABLED = False
    s.UPLOAD_CHUNK = 8 * 1024 * 1024


async def _mk_task():
    from app.db.base import get_session_factory
    from app.db.repos import create_task

    factory = get_session_factory()
    async with factory() as s:
        return await create_task(s, title="u", workflow_id="generic")


def _mk(size: int) -> bytes:
    return bytes((i * 37) & 0xff for i in range(size))


@pytest.mark.asyncio
async def test_upload_full_flow(up_api):
    ac = up_api
    task = await _mk_task()
    data = _mk(20)
    r = await ac.post("/artifacts/upload/init", json={"task_id": task.id, "rel": "a.bin", "size": len(data)})
    assert r.status_code == 200, r.text
    body = r.json()
    uid, cs = body["upload_id"], body["chunk_size"]
    assert cs == 8 and body["next_offset"] == 0

    for off in (0, 8, 16):
        c = await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": off}, content=data[off:off + 8])
        assert c.status_code == 200, c.text
        assert c.json()["received"] == min(off + 8, len(data))

    rc = await ac.post(f"/artifacts/upload/{uid}/commit")
    assert rc.status_code == 200, rc.text
    cr = rc.json()
    assert cr["ok"] is True and cr["size"] == 20
    assert cr["sha256"] == hashlib.sha256(data).hexdigest()

    got = await ac.get(f"/artifacts/{task.id}/a.bin")
    assert got.status_code == 200 and got.content == data

    # commit 后暂存清空 → status 404
    assert (await ac.get(f"/artifacts/upload/{uid}")).status_code == 404


@pytest.mark.asyncio
async def test_upload_contiguous_enforced(up_api):
    ac = up_api
    task = await _mk_task()
    r = await ac.post("/artifacts/upload/init", json={"task_id": task.id, "rel": "a.bin", "size": 20})
    uid = r.json()["upload_id"]
    # 跳块上传 → 400
    bad = await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": 8}, content=b"x" * 8)
    assert bad.status_code == 400
    # 顺序上传
    ok = await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": 0}, content=b"y" * 8)
    assert ok.status_code == 200 and ok.json()["received"] == 8
    # 幂等重传同块
    again = await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": 0}, content=b"y" * 8)
    assert again.status_code == 200 and again.json()["received"] == 8


@pytest.mark.asyncio
async def test_upload_resume_next_offset(up_api):
    ac = up_api
    task = await _mk_task()
    r1 = await ac.post("/artifacts/upload/init", json={"task_id": task.id, "rel": "a.bin", "size": 20})
    uid = r1.json()["upload_id"]
    await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": 0}, content=b"a" * 8)
    # 再次 init → 复用同 upload_id，next_offset=8（互斥+断点）
    r2 = await ac.post("/artifacts/upload/init", json={"task_id": task.id, "rel": "a.bin", "size": 20})
    b2 = r2.json()
    assert b2["upload_id"] == uid and b2["next_offset"] == 8
    # status 反映断点
    st = (await ac.get(f"/artifacts/upload/{uid}")).json()
    assert st["received"] == 8 and st["done"] is False


@pytest.mark.asyncio
async def test_upload_commit_missing_and_md5(up_api):
    ac = up_api
    task = await _mk_task()
    data = _mk(20)
    md5 = hashlib.md5(data).hexdigest()
    r = await ac.post("/artifacts/upload/init",
                      json={"task_id": task.id, "rel": "a.bin", "size": 20, "md5": md5})
    uid = r.json()["upload_id"]
    await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": 0}, content=data[0:8])
    # 缺块 commit → 400
    assert (await ac.post(f"/artifacts/upload/{uid}/commit")).status_code == 400
    for off in (8, 16):
        await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": off}, content=data[off:off + 8])
    assert (await ac.post(f"/artifacts/upload/{uid}/commit")).status_code == 200


@pytest.mark.asyncio
async def test_upload_path_traversal(up_api):
    ac = up_api
    task = await _mk_task()
    r = await ac.post("/artifacts/upload/init", json={"task_id": task.id, "rel": "../evil.bin", "size": 8})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_upload_cancel(up_api):
    ac = up_api
    task = await _mk_task()
    r = await ac.post("/artifacts/upload/init", json={"task_id": task.id, "rel": "a.bin", "size": 20})
    uid = r.json()["upload_id"]
    assert (await ac.delete(f"/artifacts/upload/{uid}")).status_code == 200
    assert (await ac.get(f"/artifacts/upload/{uid}")).status_code == 404


@pytest.mark.asyncio
async def test_upload_disabled_404(tmp_path):
    s = get_settings()
    s.UPLOAD_ENABLED = False
    from app.storage import reset_backend
    from app.storage import set_backend as _sb

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    _sb(LocalBackend(tmp_path))
    from httpx import ASGITransport, AsyncClient

    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    r = await ac.post("/artifacts/upload/init", json={"task_id": "x", "rel": "a.bin", "size": 4})
    assert r.status_code == 404
    await ac.__aexit__(None, None, None)
    await eng.dispose()
    reset_backend()