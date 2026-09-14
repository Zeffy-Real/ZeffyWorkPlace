"""P4-4 成本：usage 聚合 + 多模型计价 + 权限隔离 + CSV BOM + 缺价容错。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import repos
from app.db.base import get_session_factory, set_global_engine
from app.db.init_db import init_db
from app.main import app

SEED_A = dict(email="aa@z.io", username="alice", password="password123")
SEED_B = dict(email="bb@z.io", username="bob", password="password123")
PRICING = {"modelA": {"prompt_per_1m": 2.0, "completion_per_1m": 6.0}}


async def _client():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    await ac.__aenter__()
    return ac, eng


def _on():
    s = get_settings()
    s.AUTH_ENABLED = True
    s.REGISTRATION_ENABLED = True
    s.MODEL_PRICING = PRICING


def _off():
    s = get_settings()
    s.AUTH_ENABLED = False
    s.REGISTRATION_ENABLED = False
    s.MODEL_PRICING = {}


async def _mk_user(ac, seed):
    await ac.post("/auth/register", json=seed)
    lg = await ac.post("/auth/login", json={"email": seed["email"], "password": seed["password"]})
    return lg.json()["token"]


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


async def _seed_usage(factory, task_id: str):
    async with factory() as s:
        await repos.write_audit(s, task_id=task_id, operator="agent", action="agent_run",
                                detail={"node": "A", "model": "modelA",
                                        "result": {"usage": {"prompt_tokens": 1000,
                                                             "completion_tokens": 500}}})
        await repos.write_audit(s, task_id=task_id, operator="agent", action="agent_run",
                                detail={"node": "B", "model": "modelB",
                                        "result": {"usage": {"prompt_tokens": 2000,
                                                             "completion_tokens": 800}}})


@pytest.mark.asyncio
async def test_billing_summary_pricing_and_unknown():
    """多模型分别计价：modelA 有价、modelB 缺价记 0 + unknown 上报。"""
    ac, eng = await _client()
    try:
        _on()
        tokA = await _mk_user(ac, SEED_A)
        got = await ac.post("/tasks", json={"title": "成本任务", "workflow_id": "generic"},
                            headers=_h(tokA))
        tid = got.json()["id"]
        await _seed_usage(get_session_factory(), tid)
        r = await ac.get("/billing/summary", headers=_h(tokA))
        assert r.status_code == 200, r.text
        body = r.json()
        rows = {x["model"]: x for x in body["rows"]}
        # modelA 计价：1000/1e6*2 + 500/1e6*6 = 0.005
        assert rows["modelA"]["amount"] == pytest.approx(0.005, abs=1e-6)
        assert rows["modelA"]["total_tokens"] == 1500
        # modelB 缺价 → 0 + unknown
        assert rows["modelB"]["amount"] == 0.0
        assert "modelB" in body["unknown_price_models"]
    finally:
        await ac.__aexit__(*([None] * 3))
        await eng.dispose()
        _off()


@pytest.mark.asyncio
async def test_billing_permission_isolation():
    """🔴 权限隔离：B 只看到自己任务成本，看不到 A 的。"""
    ac, eng = await _client()
    try:
        _on()
        tokA = await _mk_user(ac, SEED_A)
        tokB = await _mk_user(ac, SEED_B)
        tidA = (await ac.post("/tasks", json={"title": "A任务", "workflow_id": "generic"},
                              headers=_h(tokA))).json()["id"]
        tidB = (await ac.post("/tasks", json={"title": "B任务", "workflow_id": "generic"},
                              headers=_h(tokB))).json()["id"]
        await _seed_usage(get_session_factory(), tidA)
        await _seed_usage(get_session_factory(), tidB)
        rb = (await ac.get("/billing/summary", headers=_h(tokB))).json()
        ra = (await ac.get("/billing/summary", headers=_h(tokA))).json()
        assert {x["task_id"] for x in rb["rows"]} == {tidB}
        assert {x["task_id"] for x in ra["rows"]} == {tidA}
    finally:
        await ac.__aexit__(*([None] * 3))
        await eng.dispose()
        _off()


@pytest.mark.asyncio
async def test_export_csv_bom():
    ac, eng = await _client()
    try:
        _on()
        tokA = await _mk_user(ac, SEED_A)
        tid = (await ac.post("/tasks", json={"title": "导出", "workflow_id": "generic"},
                             headers=_h(tokA))).json()["id"]
        await _seed_usage(get_session_factory(), tid)
        r = await ac.get("/billing/export.csv", headers=_h(tokA))
        assert r.status_code == 200
        text = r.content.decode("utf-8")
        assert text.startswith("\ufeff")  # UTF-8 BOM（Excel 兼容）
        assert "modelA" in text and "modelB" in text
    finally:
        await ac.__aexit__(*([None] * 3))
        await eng.dispose()
        _off()


@pytest.mark.asyncio
async def test_usage_rows_missing_usage_tolerant():
    """缺失 usage → 记 0，不抛错。"""
    from datetime import UTC, datetime, timedelta
    from sqlalchemy.ext.asyncio import async_sessionmaker

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as s:
        tid = (await repos.create_task(s, title="缺usage", workflow_id="generic")).id
        await repos.write_audit(s, task_id=tid, operator="agent", action="agent_run",
                                detail={"node": "A", "model": "modelA"})
        rows = await repos.usage_rows(s, since=datetime.now(UTC) - timedelta(minutes=1))
    assert rows == [{"task_id": tid, "model": "modelA",
                     "prompt_tokens": 0, "completion_tokens": 0}]
    await eng.dispose()
