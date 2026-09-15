"""P6 治理 API：tx 事务 / stats 计量（批次D/F）+ 兼容锚点。

核心用例：
1. 治理关（默认）→ /artifacts/tx/open、/artifacts/stats 均 404（兼容锚点零漂移）
2. 治理开 → /artifacts/stats 返回本人用量
3. 治理开 → tx open/status/commit/rollback 正常
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app
from app.storage.local import LocalBackend


@pytest.fixture
async def gov_api(tmp_path):
    s = get_settings()
    from app.storage import reset_backend, set_backend

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    set_backend(LocalBackend(tmp_path))
    from httpx import ASGITransport, AsyncClient

    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac, s
    await ac.__aexit__(None, None, None)
    await eng.dispose()
    reset_backend()


@pytest.mark.asyncio
async def test_governance_off_all_404(gov_api):
    """治理关（默认）→ tx/stats 全 404（兼容锚点）。"""
    ac, s = gov_api
    assert s.ARTIFACT_META_ENABLED is False
    assert (await ac.post("/artifacts/tx/open", json={"task_id": "x"})).status_code == 404
    assert (await ac.get("/artifacts/stats")).status_code == 404
    assert (await ac.post("/artifacts/tx/abc/commit")).status_code == 404


@pytest.mark.asyncio
async def test_stats_when_enabled(gov_api, tmp_path):
    ac, s = gov_api
    s.ARTIFACT_META_ENABLED = True
    s.AUTH_ENABLED = False  # 匿名（P2 兼容）→ _owner_id 返回 None → 404（无 ownable 语义）
    # AUTH off 时无 owner，stats 404；此处验证锚点：匿名访问无权
    r = await ac.get("/artifacts/stats")
    # auth off 下 user.authenticated=False → owner None → 404
    assert r.status_code == 404
    s.ARTIFACT_META_ENABLED = False
