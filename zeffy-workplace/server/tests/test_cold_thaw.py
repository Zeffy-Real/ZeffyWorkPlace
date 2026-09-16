"""P7-C1 深冷批量解冻 · 单元 + API 冒烟测试。

覆盖：
1. token 签发/校验：签名正确；篡改内容/签名 → 拒绝
2. token 绑定操作人 / 过期 / 一次性防重
3. 成本估算（数量/字节/费用/时长）
4. confirm 入队 + 复用历史 token 拒绝
5. API：开关关 404；非 admin 越权；confirm 无效 token 400
"""
from __future__ import annotations

import json
import time

import pytest

from app.config import get_settings
from app.observability import cold_thawing as CT


@pytest.fixture(autouse=True)
def _thaw_env(monkeypatch):
    s = get_settings()
    s.COLD_THAW_ENABLED = True
    s.ARTIFACT_META_ENABLED = True
    s.COLD_THAW_WM_KEY = "test-secret"
    s.COLD_THAW_EST_TOKEN_TTL = 600
    s.COLD_THAW_CONCURRENCY = 8
    s.COLD_THAW_RPS = 50
    s.COLD_THAW_MAX_RETRY = 2
    s.RESTORE_COST_PER_OBJECT = 0.01
    CT.reset_thaw_for_test()
    yield s
    s.COLD_THAW_ENABLED = False
    s.ARTIFACT_META_ENABLED = False
    s.COLD_THAW_WM_KEY = ""
    CT.reset_thaw_for_test()


def _make_token(scope="owner=u1", owner="u1", op="admin1",
                count=10, cost=0.1, exp=None):
    exp = exp or int(time.time()) + 600
    payload = json.dumps({"scope": scope, "owner": owner, "op": op,
                          "exp": exp, "count": count, "bytes": 1000,
                          "cost": cost}, separators=(",", ":"), sort_keys=True)
    import hashlib
    import hmac as _h

    sig = _h.new(b"test-secret", payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


# ---------------- token 校验 ----------------

def test_token_roundtrip(_thaw_env):
    t = _make_token()
    data = CT._resolve_token(t, operator="admin1")
    assert data is not None
    assert data["owner"] == "u1" and data["count"] == 10


def test_token_tamper_reject(_thaw_env):
    t = _make_token()
    # 篡改签名
    assert CT._resolve_token(t[:-1] + ("0" if t[-1] != "0" else "1"), operator="admin1") is None
    # 篡改内容（count）→ 签名不匹配
    tampered = _make_token(count=9999, op="admin1")
    payload_b, sig = tampered.rsplit(".", 1)
    bad = payload_b.replace('"count":9999', '"count":1') + "." + sig
    assert CT._resolve_token(bad, operator="admin1") is None


def test_token_operator_bound(_thaw_env):
    t = _make_token(op="admin1")
    assert CT._resolve_token(t, operator="admin2") is None  # 跨用户拒绝


def test_token_expired(_thaw_env):
    t = _make_token(exp=int(time.time()) - 10)
    assert CT._resolve_token(t, operator="admin1") is None


def test_token_one_time_per_scope(_thaw_env):
    t = _make_token()
    # 先入队一个同范围任务 → 再次校验同范围 token 拒绝
    job = CT.confirm(t, operator="admin1")
    assert job is not None
    t2 = _make_token()  # 同 scope/owner 的新 token
    assert CT._resolve_token(t2, operator="admin1") is None


# ---------------- 成本估算 ----------------

def test_cost_of(_thaw_env):
    c = CT._cost_of(100, 1_000_000)
    assert c["count"] == 100 and c["bytes"] == 1_000_000
    assert c["cost_usd"] == pytest.approx(1.0)  # 0.01 * 100
    assert c["duration_s"] >= 0


# ---------------- confirm ----------------

def test_confirm_queues_job(_thaw_env):
    t = _make_token()
    job = CT.confirm(t, operator="admin1")
    assert job is not None and job["status"] == "queued"
    st = CT.job_status(job["job_id"])
    assert st["job_id"] == job["job_id"]
    CT.reset_thaw_for_test()


def test_confirm_invalid_token(_thaw_env):
    assert CT.confirm("bad.token", operator="admin1") is None


def test_cancel_only_creator(_thaw_env):
    t = _make_token(op="admin1")
    job = CT.confirm(t, operator="admin1")
    assert job is not None
    assert CT.cancel_job(job["job_id"], operator="other") is False
    assert CT.cancel_job(job["job_id"], operator="admin1") is True
    CT.reset_thaw_for_test()


# ---------------- API 冒烟（admin-only + 开关） ----------------

@pytest.fixture
async def _thaw_api(tmp_path):
    from app.auth.deps import UserPrincipal, get_current_user
    from app.db.base import set_global_engine
    from app.db.init_db import init_db
    from app.main import app
    from app.storage import reset_backend, set_backend
    from app.storage.local import LocalBackend

    s = get_settings()
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    set_backend(LocalBackend(tmp_path))
    s.ARTIFACT_META_ENABLED = True
    s.COLD_THAW_ENABLED = True
    s.COLD_THAW_WM_KEY = "test-secret"

    async def _admin():
        return UserPrincipal(id="a1", username="admin", role="admin")

    async def _anon():
        return UserPrincipal(id=None, username=None, is_system=False, role="user")

    app.dependency_overrides[get_current_user] = _admin
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac, s, app, get_current_user, _anon, eng
    await ac.__aexit__(None, None, None)
    app.dependency_overrides.clear()
    await eng.dispose()
    reset_backend()
    s.ARTIFACT_META_ENABLED = False
    s.COLD_THAW_ENABLED = False
    s.COLD_THAW_WM_KEY = ""


@pytest.mark.asyncio
async def test_api_thaw_disabled_404(_thaw_api):
    ac, s, *_ = _thaw_api
    s.COLD_THAW_ENABLED = False
    r = await ac.post("/admin/governance/thaw/estimate", json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_thaw_non_admin_404(_thaw_api):
    ac, s, app, dep, anon, _ = _thaw_api
    app.dependency_overrides[dep] = anon
    r = await ac.post("/admin/governance/thaw/estimate", json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_thaw_estimate_admin_ok(_thaw_api):
    ac, s, *_ = _thaw_api
    r = await ac.post("/admin/governance/thaw/estimate", json={"owner_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["scope"].startswith("owner=")
    assert "estimate_token" in body and "cost_usd" in body


@pytest.mark.asyncio
async def test_api_thaw_confirm_invalid_400(_thaw_api):
    ac, s, *_ = _thaw_api
    r = await ac.post("/admin/governance/thaw/confirm", json={"token": "x.y"})
    assert r.status_code in (400, 503)
