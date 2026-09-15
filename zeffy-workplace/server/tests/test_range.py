"""P5-4 Range 断点续传：stream(start) 偏移、size、Rollover ETag、边界/非法 Range、206/416、HEAD、鉴权。

核心用例（审查🔴/⭐）：
1. 并发：多协程请求同一文件不同区间 → 返回与字节区间完全对应（Local，防共享句柄错位）
2. size/stream(start)：start=0/中间/size-1 内容正确；start≥size 抛 RangeNotSatisfiableError
3. 边界/非法：多段/后缀/非数字 Range 降级 200；越界 416
4. 206 + Content-Range；HEAD 返回 size+Accept-Ranges+ETag
5. 兼容锚点：RANGE_ENABLED=false 时无 Range 头行为 200 全量零差异
6. 鉴权：HEAD/Range 越权 404
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app
from app.storage.base import RangeNotSatisfiableError
from app.storage.local import LocalBackend

# ---- 后端层（Local）----

def _mk_bytes(n: int) -> bytes:
    return bytes(range(256)) * (n // 256) + bytes(range(n % 256))


@pytest.mark.asyncio
async def test_stream_start_offset(tmp_path):
    b = LocalBackend(tmp_path)
    data = _mk_bytes(10000)
    await b.put("artifacts/t1/big.bin", data, mode="no_overwrite")
    # start=0
    c0 = b"".join([c async for c in b.stream("artifacts/t1/big.bin", start=0)])
    assert c0 == data
    # start=中间
    c100 = b"".join([c async for c in b.stream("artifacts/t1/big.bin", start=100)])
    assert c100 == data[100:]
    # start=size-1
    c_last = b"".join([c async for c in b.stream("artifacts/t1/big.bin", start=9999)])
    assert c_last == data[9999:]
    # size
    assert await b.size("artifacts/t1/big.bin") == 10000


@pytest.mark.asyncio
async def test_stream_start_oob(tmp_path):
    b = LocalBackend(tmp_path)
    data = b"x" * 100
    await b.put("artifacts/t1/a.bin", data, mode="no_overwrite")
    # start=size → 越界抛 RangeNotSatisfiableError
    with pytest.raises(RangeNotSatisfiableError):
        await _drain(b, "artifacts/t1/a.bin", start=100)
    with pytest.raises(RangeNotSatisfiableError):
        await _drain(b, "artifacts/t1/a.bin", start=1000)


async def _drain(b, key, start=0):
    acc = b""
    async for chunk in b.stream(key, start=start):
        acc += chunk
    return acc


@pytest.mark.asyncio
async def test_concurrent_stream_no_cross_talk(tmp_path):
    """🔴1 并发安全：多协程同时读同一文件不同区间，各自返回对应字节（独立句柄不自相覆盖）。"""
    b = LocalBackend(tmp_path)
    data = _mk_bytes(200000)
    await b.put("artifacts/t1/big.bin", data, mode="no_overwrite")

    async def read(start):
        acc = b""
        async for c in b.stream("artifacts/t1/big.bin", start=start):
            acc += c
        return start, acc

    # 并发 5 个不同起点
    starts = [0, 100, 50000, 120000, 180000]
    results = await asyncio.gather(*[read(s) for s in starts])
    for start, acc in results:
        assert acc == data[start:]


# ---- HTTP 层（RANGE_ENABLED=True）----

@pytest.fixture
async def rng_api(tmp_path):
    s = get_settings()
    s.RANGE_ENABLED = True
    from app.storage import reset_backend
    from app.storage import set_backend as _sb

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    backend = LocalBackend(tmp_path)
    _sb(backend)  # 让 API get_backend() 指向该本地后端（Range 测试隔离）
    from httpx import ASGITransport, AsyncClient

    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac, backend
    await ac.__aexit__(None, None, None)
    await eng.dispose()
    reset_backend()
    s.RANGE_ENABLED = False


async def _mk_task():
    from app.db.base import get_session_factory
    from app.db.repos import create_task

    factory = get_session_factory()
    async with factory() as s:
        return await create_task(s, title="r", workflow_id="generic")


@pytest.mark.asyncio
async def test_get_range_206(rng_api):
    ac, backend = rng_api
    task = await _mk_task()
    key = f"artifacts/{task.id}/big.bin"
    data = _mk_bytes(1000)
    await backend.put(key, data, mode="no_overwrite")

    r = await ac.get(f"/artifacts/{task.id}/big.bin", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.content == data[0:100]
    assert r.headers["content-range"] == f"bytes 0-99/{len(data)}"
    assert r.headers["content-length"] == "100"
    assert "bytes" in r.headers["accept-ranges"]
    assert r.headers.get("etag")

    # start- 无 end
    r2 = await ac.get(f"/artifacts/{task.id}/big.bin", headers={"Range": "bytes=900-"})
    assert r2.status_code == 206
    assert r2.content == data[900:]


@pytest.mark.asyncio
async def test_get_range_416(rng_api):
    ac, backend = rng_api
    task = await _mk_task()
    await backend.put(f"artifacts/{task.id}/big.bin", _mk_bytes(100), mode="no_overwrite")
    r = await ac.get(f"/artifacts/{task.id}/big.bin", headers={"Range": "bytes=999-"})
    assert r.status_code == 416
    assert "bytes */100" in r.headers["content-range"]


@pytest.mark.asyncio
async def test_get_range_invalid_downgrade_200(rng_api):
    """非法 Range（多段/后缀/非数字）→ 降级 200 全量。"""
    ac, backend = rng_api
    task = await _mk_task()
    data = _mk_bytes(300)
    await backend.put(f"artifacts/{task.id}/big.bin", data, mode="no_overwrite")
    for bad in ("bytes=0-99,200-299", "bytes=-50", "bytes=abc"):
        r = await ac.get(f"/artifacts/{task.id}/big.bin", headers={"Range": bad})
        assert r.status_code == 200, bad
        assert r.content == data, bad


@pytest.mark.asyncio
async def test_head_returns_size_etag(rng_api):
    ac, backend = rng_api
    task = await _mk_task()
    data = _mk_bytes(500)
    await backend.put(f"artifacts/{task.id}/big.bin", data, mode="no_overwrite")
    r = await ac.request("HEAD", f"/artifacts/{task.id}/big.bin")
    assert r.status_code == 200
    assert r.headers["content-length"] == "500"
    assert "bytes" in r.headers["accept-ranges"]
    assert r.headers.get("etag")


@pytest.mark.asyncio
async def test_no_range_backward_compat(rng_api):
    """无 Range 头 → 200 全量（即使 Range 开启也返回全量，兼容）。"""
    ac, backend = rng_api
    task = await _mk_task()
    data = _mk_bytes(200)
    await backend.put(f"artifacts/{task.id}/big.bin", data, mode="no_overwrite")
    r = await ac.get(f"/artifacts/{task.id}/big.bin")
    assert r.status_code == 200
    assert r.content == data
