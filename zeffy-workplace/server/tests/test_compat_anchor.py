"""P6 审查闭环 · 兼容锚点深度矩阵（🔴7：关闭态 P5 零漂移）。

核心断言：``ARTIFACT_META_ENABLED=false`` 时，治理各函数短路为 no-op——不写元表、
不记配额、不建会话改库；底层存储 put/get/delete/list 行为与 P5 完全一致。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.storage.local import LocalBackend


@pytest.fixture
async def off(tmp_path):
    """关闭态：治理总开关 false（默认即 false），P5 行为基线。"""
    s = get_settings()
    s.ARTIFACT_META_ENABLED = False
    s.TX_ENABLED = False
    s.QUOTA_ENABLED = False
    s.TIER_ENABLED = False
    s.RECYCLE_ENABLED = False
    from app.storage import reset_backend, set_backend

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    backend = LocalBackend(tmp_path)
    set_backend(backend)
    yield s
    await eng.dispose()
    reset_backend()


@pytest.mark.asyncio
async def test_meta_disabled_no_meta_no_quota(off):
    """关闭态：record_artifact_meta / check_quota / reserve / tx 全 no-op。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_artifacts_by_key
    from app.storage import get_backend
    from app.storage.governance import (
        account_quota,
        check_quota,
        record_artifact_meta,
        release_quota,
        tx_open,
    )

    backend = get_backend()
    key = "artifacts/t1/doc.md"
    await backend.put(key, b"# hello", mode="overwrite")
    # 治理 no-op：不建元表、不记配额
    await record_artifact_meta(task_id="t1", rel_path="doc.md", key=key,
                               owner_id="u1", size=8, backend="local")
    await check_quota(owner_id="u1", size=1000)  # 应直接返回（超配也不拦）
    await account_quota(owner_id="u1", size=100)
    await release_quota(owner_id="u1", size=50)
    async with get_session_factory()() as session:
        assert await get_artifacts_by_key(session, key=key) == []  # 无元表
    # 事务关闭：tx 直接抛错（能力关）
    with pytest.raises(RuntimeError):
        await tx_open(task_id="t1", owner_id="u1")
    # 读取 P5 行为一致
    assert await backend.get(key) == b"# hello"


@pytest.mark.asyncio
async def test_meta_disabled_storage_drifts_and_functions(off):
    """关闭态：存储三态 / list / delete 行为与 P5 一致（无治理介入）。"""
    from app.storage import get_backend

    backend = get_backend()
    key = "artifacts/t2/a.md"
    await backend.put(key, b"v1", mode="overwrite")
    # no_overwrite 拒绝
    from app.storage.base import FileExistsError_

    with pytest.raises(FileExistsError_):
        await backend.put(key, b"v2", mode="no_overwrite")
    got = await backend.get(key)
    assert got == b"v1"
    keys = await backend.list("artifacts/t2/")
    assert key in keys
    assert await backend.delete(key) is True
    assert not await backend.exists(key)


@pytest.mark.asyncio
async def test_meta_disabled_o1_o2_short_circuit(off):
    """P6-2 O1/O2：总闸关闭时 热度埋点/配额采样/报表 全 no-op 且不建历史、不改元表。"""
    from sqlalchemy import func, select

    from app.db.base import get_session_factory
    from app.db.models import QuotaHistory
    from app.storage import get_backend
    from app.storage.governance import (
        quota_history_sweep_once,
        quota_report_for,
        record_artifact_meta,
        touch_artifact,
    )

    backend = get_backend()
    await backend.put("artifacts/t3/a.md", b"# x", mode="overwrite")
    await record_artifact_meta(task_id="t3", rel_path="a.md", key="artifacts/t3/a.md",
                               owner_id="u1", size=3, backend="local")
    # O1：总闸关闭 → touch 不落库（即便分段开关开着也不影响）
    await touch_artifact(task_id="t3", rel_path="a.md")
    from app.db.repos import get_artifact_by_rel

    factory = get_session_factory()
    async with factory() as session:
        rec = await get_artifact_by_rel(session, task_id="t3", rel_path="a.md")
    if rec is not None:
        assert rec.last_access is None and rec.access_count == 0  # 不埋点不计数
    # O2：采样 no-op + 报表为空
    r = await quota_history_sweep_once(factory)
    assert r.get("enabled") is False
    assert await quota_report_for("u1") == {}
    # 未产生任何历史采样
    async with factory() as session:
        cnt = await session.scalar(
            select(func.count()).select_from(QuotaHistory))
    assert int(cnt or 0) == 0
