"""P4 trace_id 贯穿：HTTP X-Trace-ID 透传回传 + 审计落库同 trace。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import repos
from app.db.base import get_session_factory, set_global_engine
from app.db.init_db import init_db
from app.main import app


async def _mk_client():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    await ac.__aenter__()
    return ac, eng


@pytest.mark.asyncio
async def test_trace_header_echo_and_audit_same_trace():
    ac, eng = await _mk_client()
    try:
        s = __import__("app.config", fromlist=["get_settings"]).get_settings()
        s.AUTH_ENABLED = True
        s.REGISTRATION_ENABLED = True
        await ac.post("/auth/register", json={"email": "t@z.io", "username": "tracer",
                                              "password": "password123"})
        trace = "abc123def456"
        r = await ac.post("/auth/login",
                          headers={"X-Trace-ID": trace},
                          json={"email": "t@z.io", "password": "password123"})
        assert r.status_code == 200
        # 🔴 响应头回传同一 trace
        assert r.headers.get("X-Trace-ID") == trace
        # 审计 action=login 落库带同一 trace_id
        f = get_session_factory()
        async with f() as s2:
            logs = await repos.list_audit(s2, action="login")
        assert logs and all(x.trace_id == trace for x in logs), \
            [x.trace_id for x in logs]
    finally:
        await ac.__aexit__(*([None] * 3))
        await eng.dispose()
        s = __import__("app.config", fromlist=["get_settings"]).get_settings()
        s.AUTH_ENABLED = False
        s.REGISTRATION_ENABLED = False


@pytest.mark.asyncio
async def test_trace_auto_generated_when_absent():
    ac, eng = await _mk_client()
    try:
        r = await ac.get("/health")
        # 未带 header → 自动生成并回传
        trace = r.headers.get("X-Trace-ID")
        assert trace and len(trace) > 16
    finally:
        await ac.__aexit__(*([None] * 3))
        await eng.dispose()
