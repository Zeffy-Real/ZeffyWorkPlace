"""P4-4b 优先级：队列选择 / 高优权限 / LLM 分级配额保底。"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.appstate import init_llm_semaphore, llm_quota
from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app
from app.queue.priorities import all_queues, queue_for

SEED = dict(email="p@z.io", username="plan", password="password123")


def _on(prior=True):
    s = get_settings()
    s.ARQ_PRIORITY_ENABLED = prior
    s.AUTH_ENABLED = True
    s.REGISTRATION_ENABLED = True


def _reset():
    s = get_settings()
    s.ARQ_PRIORITY_ENABLED = False
    s.AUTH_ENABLED = False
    s.REGISTRATION_ENABLED = False


def test_queue_for_mapping():
    s = get_settings()
    s.ARQ_PRIORITY_ENABLED = True
    assert queue_for(0) == "zeffy_lo"
    assert queue_for(1) == "zeffy"
    assert queue_for(2) == "zeffy_hi"
    assert all_queues() == {"zeffy", "zeffy_hi", "zeffy_lo"}
    s.ARQ_PRIORITY_ENABLED = False
    assert queue_for(2) == "zeffy"  # 关 → 一律基队列（P4 兼容）
    s.ARQ_PRIORITY_ENABLED = True


@pytest.mark.asyncio
async def test_llm_quota_low_backstop_not_starved():
    """🔴 资源保底：高/中优占满 LLM 配额时，低优仍能借到 lo 保底推进。"""
    s = get_settings()
    s.LLM_QUOTA_HI = 1
    s.LLM_QUOTA_MID = 1
    s.LLM_QUOTA_LO = 1
    s.LLM_QUOTA_PUBLIC = 1
    init_llm_semaphore(max_concurrency=4)

    # 高优占满 hi 与 pub
    async with llm_quota(2):
        async with llm_quota(2):  # hi 满 → 借 pub1 成功
            async with llm_quota(1):
                # 中优占满 mid → 借 pub2 失败（pub 已被占）→ 应阻塞等待，但这里用 lo 应在高优占用下仍能进入
                pass
            # 低优用 lo 保底，即使 hi/mid/pub 全被占也能 acquire
            lo_done = await asyncio.wait_for(
                _acquire(0), timeout=1.0
            )
    assert lo_done is True, "低优在资源被占满时应仍能靠 lo 保底获取执行资源"
    s.LLM_QUOTA_HI = 5
    s.LLM_QUOTA_MID = 3
    s.LLM_QUOTA_LO = 2
    s.LLM_QUOTA_PUBLIC = 0
    init_llm_semaphore()


async def _acquire(priority: int) -> bool:
    async with llm_quota(priority):
        return True


async def _mk_user(ac):
    await ac.post("/auth/register", json=SEED)
    lg = await ac.post("/auth/login", json={"email": SEED["email"], "password": SEED["password"]})
    return lg.json()["token"]


@pytest.mark.asyncio
async def test_high_priority_requires_admin():
    """🔴 普通用户 priority=2 → 403；中/低 → 200（AUTH off 放行）。"""
    _on()
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        tok = await _mk_user(ac)
        h = {"Authorization": f"Bearer {tok}"}
        r_hi = await ac.post("/tasks", json={"title": "高优", "priority": 2,
                                             "workflow_id": "generic"}, headers=h)
        assert r_hi.status_code == 403, r_hi.text
        r_mid = await ac.post("/tasks", json={"title": "中", "priority": 1,
                                              "workflow_id": "generic"}, headers=h)
        assert r_mid.status_code == 200
    _reset()
    await eng.dispose()


@pytest.mark.asyncio
async def test_auth_off_allows_high_priority():
    """AUTH off（匿名）可提交高优（无管理员体系，兼容 P4 调试）。"""
    _on()
    get_settings().AUTH_ENABLED = False
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/tasks", json={"title": "高优", "priority": 2,
                                          "workflow_id": "generic"})
        assert r.status_code == 200
        assert r.json()["priority"] == 2
    _reset()
    await eng.dispose()
