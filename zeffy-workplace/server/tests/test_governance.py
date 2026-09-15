"""P6 产物生命周期治理：权威元表（批次B）/ 配额（批次C）/ 事务批次（批次D）。

核心用例（审查🔴/⭐）：
1. 元表记录：开启治理 → record_artifact_meta 落一条 artifact + 配额记账（🔴1/🔴3）
2. 治理关 no-op：ARTIFACT_META_ENABLED=false 不写元表（兼容锚点零漂移）
3. 配额拦截：超 total 拒绝（🔴3）；删除/回滚冲正（🔴5）
4. 配额豁免：system 账号跳过
5. 事务：open→记录多文件→commit；回滚删 key + 冲正（🔴4）
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.storage.local import LocalBackend


class _MiniSettings:  # 复用 get_settings 会污染全局，改用真实 settings 开关控制
    pass


@pytest.fixture
async def gov(tmp_path):
    """隔离环境：内存 DB(含 P6 三表) + LocalBackend，治理全开。"""
    s = get_settings()
    s.ARTIFACT_META_ENABLED = True
    s.QUOTA_ENABLED = True
    s.QUOTA_ASSET_MAX_BYTES = 0
    s.QUOTA_TOTAL_MAX_BYTES = 0
    s.QUOTA_EXEMPT_SYSTEM = False
    s.TIER_ENABLED = False
    from app.storage import reset_backend, set_backend

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    backend = LocalBackend(tmp_path)
    set_backend(backend)
    yield s
    await eng.dispose()
    reset_backend()
    s.ARTIFACT_META_ENABLED = False
    s.QUOTA_ENABLED = False
    s.QUOTA_TOTAL_MAX_BYTES = 0


# ---- 批次 B：元表记录 ----

@pytest.mark.asyncio
async def test_record_artifact_creates_row(gov):
    from app.db.base import get_session_factory
    from app.db.repos import list_artifacts
    from app.storage.governance import record_artifact_meta

    await record_artifact_meta(
        task_id="t1", rel_path="doc.md", key="artifacts/t1/doc.md",
        owner_id="u1", size=10, backend="local", sha256="abc", mime="text/markdown",
    )
    factory = get_session_factory()
    async with factory() as session:
        rows, total = await list_artifacts(session, owner_id="u1")
    assert total == 1 and rows[0].rel_path == "doc.md"
    assert rows[0].tier == "hot" and rows[0].status == "available"


@pytest.mark.asyncio
async def test_governance_disabled_noop(tmp_path):
    """ARTIFACT_META_ENABLED=false 不写元表（兼容锚点零漂移）。"""
    from app.config import get_settings
    from app.db.base import get_session_factory, set_global_engine
    from app.db.repos import list_artifacts
    from app.storage.governance import record_artifact_meta

    s = get_settings()
    assert s.ARTIFACT_META_ENABLED is False  # 默认关

    from app.storage import reset_backend, set_backend
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    set_backend(LocalBackend(tmp_path))

    await record_artifact_meta(
        task_id="t1", rel_path="a.md", key="artifacts/t1/a.md",
        owner_id="u1", size=5, backend="local",
    )
    factory = get_session_factory()
    async with factory() as session:
        _, total = await list_artifacts(session, owner_id="u1")
    assert total == 0  # 治理关 → 不记录
    await eng.dispose()
    reset_backend()


# ---- 批次 C：配额 ----

@pytest.mark.asyncio
async def test_quota_total_intercept_and_release(gov):
    from app.storage.governance import (
        QuotaExceededError,
        account_quota,
        check_quota,
        release_quota,
    )
    gov.QUOTA_TOTAL_MAX_BYTES = 100
    await account_quota(owner_id="u1", size=80)
    await check_quota(owner_id="u1", size=20)  # 80+20=100 不超
    with pytest.raises(QuotaExceededError):
        await check_quota(owner_id="u1", size=50)  # 80+50=130 超
    await release_quota(owner_id="u1", size=80)  # 冲正
    await check_quota(owner_id="u1", size=50)  # 已释放可再写


@pytest.mark.asyncio
async def test_quota_asset_limit(gov):
    from app.storage.governance import QuotaExceededError, check_quota
    gov.QUOTA_ASSET_MAX_BYTES = 10
    await check_quota(owner_id="u1", size=10)
    with pytest.raises(QuotaExceededError):
        await check_quota(owner_id="u1", size=11)


@pytest.mark.asyncio
async def test_quota_system_exempt(gov):
    from app.db.base import get_session_factory
    from app.db.repos import get_quota_used as _repo
    from app.storage.governance import account_quota
    gov.QUOTA_TOTAL_MAX_BYTES = 0
    gov.QUOTA_EXEMPT_SYSTEM = True
    await account_quota(owner_id="system", size=9999)
    factory = get_session_factory()
    async with factory() as session:
        used = await _repo(session, owner_id="system")
    assert used == 0  # system 豁免，不记账


# ---- 批次 D：事务 ----

@pytest.mark.asyncio
async def test_tx_open_commit_roundtrip(gov):
    from app.storage.governance import tx_commit, tx_open, tx_status

    info = await tx_open(task_id="t1", owner_id="u1")
    tx_id = info["tx_id"]
    st = await tx_status(tx_id=tx_id)
    assert st["status"] == "pending" and st["files"] == 0
    r = await tx_commit(tx_id=tx_id)
    assert r["status"] == "committed"
    assert (await tx_status(tx_id=tx_id))["status"] == "committed"


@pytest.mark.asyncio
async def test_tx_rollback_cleans_artifacts(gov):
    from app.db.base import get_session_factory
    from app.db.repos import list_artifacts
    from app.storage import get_backend
    from app.storage.governance import record_artifact_meta, tx_open, tx_rollback

    backend = get_backend()
    info = await tx_open(task_id="t2", owner_id="u1")
    tx_id = info["tx_id"]
    key1 = "artifacts/t2/a.md"
    key2 = "artifacts/t2/b.md"
    await backend.put(key1, b"aaa", mode="overwrite")
    await backend.put(key2, b"bbbb", mode="overwrite")
    await record_artifact_meta(task_id="t2", rel_path="a.md", key=key1,
                               owner_id="u1", size=3, backend="local", tx_id=tx_id)
    await record_artifact_meta(task_id="t2", rel_path="b.md", key=key2,
                               owner_id="u1", size=4, backend="local", tx_id=tx_id)

    r = await tx_rollback(tx_id=tx_id)
    assert r["status"] == "rolled_back" and r["files"] == 2
    assert await backend.get(key1) is None
    assert await backend.get(key2) is None
    factory = get_session_factory()
    async with factory() as session:
        _, total = await list_artifacts(session, owner_id="u1")
    assert total == 0  # 元表清空


# ---- 批次 E：存储分层 ----

@pytest.mark.asyncio
async def test_tier_archive_local_real_move(gov):
    """Local 冷化：物理文件移 _cold，读路由透明，元表 tier=cold。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_artifact_by_rel
    from app.storage import get_backend
    from app.storage.governance import tier_archive

    gov.TIER_ENABLED = True
    backend = get_backend()
    key = "artifacts/t9/doc.md"
    await backend.put(key, b"# cold me", mode="overwrite")
    from app.storage.governance import record_artifact_meta
    await record_artifact_meta(
        task_id="t9", rel_path="doc.md", key=key,
        owner_id="u1", size=9, backend="local",
    )
    # 冷化前 hot 文件存在
    assert (backend.root / "artifacts/t9" / "doc.md").is_file()

    r = await tier_archive(task_id="t9", rel_path="doc.md")
    assert r["ok"] is True and r["tier"] == "cold"
    # hot 已移走，cold 目录有文件
    assert not (backend.root / "artifacts/t9" / "doc.md").exists()
    assert (backend.root / "_cold/t9" / "doc.md").is_file()
    # 读路由透明：key 仍可读
    assert await backend.get(key) == b"# cold me"
    # 元表 tier=cold
    factory = get_session_factory()
    async with factory() as session:
        rec = await get_artifact_by_rel(session, task_id="t9", rel_path="doc.md")
    assert rec is not None and rec.tier == "cold"
