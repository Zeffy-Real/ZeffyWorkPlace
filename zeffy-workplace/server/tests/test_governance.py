"""P6 产物生命周期治理：权威元表（批次B）/ 配额（批次C）/ 事务批次（批次D）。

核心用例（审查🔴/⭐）：
1. 元表记录：开启治理 → record_artifact_meta 落一条 artifact + 配额记账（🔴1/🔴3）
2. 治理关 no-op：ARTIFACT_META_ENABLED=false 不写元表（兼容锚点零漂移）
3. 配额拦截：超 total 拒绝（🔴3）；删除/回滚冲正（🔴5）
4. 配额豁免：system 账号跳过
5. 事务：open→记录多文件→commit；回滚删 key + 冲正（🔴4）
6. 批次 G 审查闭环：统一删除编排；对账；版本同步；存量初始化
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
    s.TX_ENABLED = True
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
    s.TX_ENABLED = False


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
    """🔴4 提交原子可见：暂存对外不可读，commit 后最终 key 可见。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_artifacts_by_key
    from app.storage import get_backend
    from app.storage.governance import tx_commit, tx_open, tx_stage_write, tx_status

    backend = get_backend()
    info = await tx_open(task_id="t1", owner_id="u1")
    tx_id = info["tx_id"]
    st = await tx_status(tx_id=tx_id)
    assert st["status"] == "pending" and st["files"] == 0
    await tx_stage_write(tx_id=tx_id, task_id="t1", rel_path="a.md", data=b"# hello")
    # 暂存不可读、不可列出
    assert await backend.get("artifacts/_tx/any/omit") is None
    key_list = await backend.list("artifacts/t1/")
    assert all("_tx" not in k for k in key_list)
    # 提交后最终 key 可见、暂存消失
    r = await tx_commit(tx_id=tx_id)
    assert r["status"] == "committed" and r["files"] == 1
    assert await backend.get("artifacts/t1/a.md") == b"# hello"
    factory = get_session_factory()
    async with factory() as session:
        rows = await get_artifacts_by_key(session, key="artifacts/t1/a.md")
    assert rows and rows[0].status == "available"
    assert (await tx_status(tx_id=tx_id))["status"] == "committed"


@pytest.mark.asyncio
async def test_tx_rollback_cleans_artifacts(gov):
    """🔴4/🔴6 回滚：删暂存文件 + pending 元表 + 返还预扣，无残留。"""
    from app.storage import get_backend
    from app.storage.governance import tx_open, tx_rollback, tx_stage_write

    backend = get_backend()
    info = await tx_open(task_id="t2", owner_id="u1")
    tx_id = info["tx_id"]
    await tx_stage_write(tx_id=tx_id, task_id="t2", rel_path="a.md", data=b"aaa")
    await tx_stage_write(tx_id=tx_id, task_id="t2", rel_path="b.md", data=b"bbbb")

    r = await tx_rollback(tx_id=tx_id)
    assert r["status"] == "rolled_back" and r["files"] == 2
    # 最终 key 未出现，暂存文件已物理删除
    assert await backend.get("artifacts/t2/a.md") is None
    assert not (backend.root / "artifacts" / "_tx" / tx_id / "t2" / "a.md").exists()
    assert not (backend.root / "artifacts" / "_tx" / tx_id / "t2" / "b.md").exists()


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


# ===========================================================================
# P6-2 O1 · 智能分层（热度埋点 + 按访问频率冷化 + 冷却期防抖）
# ===========================================================================

async def _set_last_access(artifact_id: str, value):
    """直接改元表 last_access（时间相关断言用，避免引入时钟库）。"""
    from sqlalchemy import update

    from app.db.base import get_session_factory
    from app.db.models import Artifact

    async with get_session_factory()() as session:
        await session.execute(
            update(Artifact).where(Artifact.id == artifact_id)
            .values(last_access=value if value is not None else None))
        await session.commit()


@pytest.mark.asyncio
async def test_touch_artifact_updates_last_access(gov):
    """完整读 → last_access 刷新 + access_count 累计；短时重复读被 TTL 节流跳过。"""
    from datetime import UTC, datetime

    from app.db.base import get_session_factory
    from app.db.repos import get_artifact_by_rel
    from app.storage import get_backend
    from app.storage.governance import record_artifact_meta, touch_artifact

    gov.TIER_ENABLED = True
    backend = get_backend()
    await backend.put("artifacts/t11/a.md", b"# hot", mode="overwrite")
    await record_artifact_meta(task_id="t11", rel_path="a.md", key="artifacts/t11/a.md",
                               owner_id="u1", size=5, backend="local")
    before = datetime.now(UTC).replace(tzinfo=None)  # sqlite 落库为 naive UTC
    await touch_artifact(task_id="t11", rel_path="a.md")
    factory = get_session_factory()
    async with factory() as session:
        rec = await get_artifact_by_rel(session, task_id="t11", rel_path="a.md")
    assert rec is not None and rec.access_count == 1
    assert rec.last_access is not None and rec.last_access >= before

    # 短时重复完整读：TTL 内跳过，access_count 不变
    await touch_artifact(task_id="t11", rel_path="a.md")
    async with factory() as session:
        rec = await get_artifact_by_rel(session, task_id="t11", rel_path="a.md")
    assert rec.access_count == 1
    assert rec.last_access is not None


@pytest.mark.asyncio
async def test_cold_sweep_uses_last_access_with_cool_down(gov):
    """冷化按 last_access；最近访问(冷却期内)不冷化，冷却期外冷化。"""
    from datetime import UTC, datetime, timedelta

    from app.db.base import get_session_factory
    from app.db.repos import get_artifact_by_rel
    from app.storage import get_backend
    from app.storage.governance import cold_sweep_once, record_artifact_meta

    gov.TIER_ENABLED = True
    gov.TIER_COLD_ACCESS_AGE = 0  # 依赖冷却期判定活度
    gov.TIER_COOL_DOWN = 3600
    backend = get_backend()
    # 热文件 hot1：最近访问（冷却期内）→ 不冷化
    await backend.put("artifacts/t12/h1.md", b"# fresh", mode="overwrite")
    await record_artifact_meta(task_id="t12", rel_path="h1.md", key="artifacts/t12/h1.md",
                               owner_id="u1", size=7, backend="local")
    factory = get_session_factory()
    async with factory() as session:
        r1 = await get_artifact_by_rel(session, task_id="t12", rel_path="h1.md")
        await _set_last_access(r1.id, datetime.now(UTC))
    # 冷文件 warm1：很久未访问（冷却期外）→ 应冷化
    await backend.put("artifacts/t12/w1.md", b"# cold me", mode="overwrite")
    await record_artifact_meta(task_id="t12", rel_path="w1.md", key="artifacts/t12/w1.md",
                               owner_id="u1", size=8, backend="local")
    async with factory() as session:
        r2 = await get_artifact_by_rel(session, task_id="t12", rel_path="w1.md")
        await _set_last_access(r2.id, datetime.now(UTC) - timedelta(days=3))

    n = await cold_sweep_once(factory, backend)
    # 仅冷化 warm1（冷却期外）
    async with factory() as session:
        s1 = await get_artifact_by_rel(session, task_id="t12", rel_path="h1.md")
        s2 = await get_artifact_by_rel(session, task_id="t12", rel_path="w1.md")
    assert s1.tier == "hot"      # 冷却期内不冷化
    assert s2.tier == "cold"     # 冷却期外冷化
    assert n >= 1


@pytest.mark.asyncio
async def test_cold_sweep_fallback_created_at(gov):
    """存量无 last_access(IS NULL) → 回退 created_at 判定。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_artifact_by_rel
    from app.storage import get_backend
    from app.storage.governance import cold_sweep_once, record_artifact_meta

    gov.TIER_ENABLED = True
    gov.TIER_COLD_ACCESS_AGE = 60
    gov.TIER_COOL_DOWN = 0
    backend = get_backend()
    # 新写产物 last_access 为空 → 回退 created_at（新鲜，不冷化）
    await backend.put("artifacts/t13/n.md", b"# new", mode="overwrite")
    await record_artifact_meta(task_id="t13", rel_path="n.md", key="artifacts/t13/n.md",
                               owner_id="u1", size=5, backend="local")
    n = await cold_sweep_once(get_session_factory(), backend)
    factory = get_session_factory()
    async with factory() as session:
        rec = await get_artifact_by_rel(session, task_id="t13", rel_path="n.md")
    assert rec.tier == "hot" and n == 0  # 存量默认不回填，created_at 新鲜不可冷化


# ===========================================================================
# P6-2 O2 · 配额智能（历史采样 + 趋势预测 + 报表）
# ===========================================================================

@pytest.mark.asyncio
async def test_quota_history_sweep_and_report(gov):
    """采样：owner 落历史；报表含趋势/峰值/建议/成本；负斜率不预测。"""
    from datetime import UTC, datetime, timedelta

    from app.db import repos
    from app.db.base import get_session_factory
    from app.storage.governance import (
        _quota_trend,
        quota_history_sweep_once,
        quota_report_for,
    )

    gov.QUOTA_ENABLED = True
    gov.QUOTA_TOTAL_MAX_BYTES = 1000
    gov.QUOTA_HISTORY_ENABLED = True

    # 写一条配额用量
    factory = get_session_factory()
    async with factory() as session:
        await repos.bump_quota(session, owner_id="u1", delta=500)

    r = await quota_history_sweep_once(factory)
    assert r["sampled"] == 1

    # 报表字段齐全
    report = await quota_report_for("u1")
    assert report["quota_used"] == 500
    assert report["quota_total"] == 1000
    assert report["trend"] is None  # 单点样本 <2 → 不可预测
    assert report["peak"]["used_bytes"] == 500
    assert set(report) >= {"suggestions", "cost"}

    # 负斜率/下降 → 不做耗尽预测（stable）
    history = [(datetime.now(UTC) - timedelta(hours=4 - i), 500 - i * 100)
               for i in range(5)]
    trend = _quota_trend(history, 1000)
    assert trend is not None and trend["eta_hours"] is None

    # 增长趋势 → 给出 ETA
    history_up = [(datetime.now(UTC) - timedelta(minutes=10 * (9 - i)), 100 + i * 20)
                  for i in range(10)]
    trend_up = _quota_trend(history_up, 1000)
    assert trend_up is not None and trend_up["trend"] == "growing"
    assert trend_up["eta_hours"] is not None


# ===========================================================================
# 批次 G · 审查闭环：统一删除编排 / 对账 / 版本同步 / 存量初始化
# ===========================================================================

@pytest.mark.asyncio
async def test_delete_artifact_governed_order_and_quota(gov):
    """统一删除编排（🔴5）：先删存储→删元表→冲正配额；文件与记录都不残留。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_artifacts_by_key
    from app.storage import get_backend
    from app.storage.governance import delete_artifact_governed, record_artifact_meta

    gov.QUOTA_ENABLED = True
    backend = get_backend()
    key = "artifacts/t10/doc.md"
    await backend.put(key, b"hello delete", mode="overwrite")
    await record_artifact_meta(
        task_id="t10", rel_path="doc.md", key=key,
        owner_id="u1", size=12, backend="local",
    )
    factory = get_session_factory()
    async with factory() as session:
        rows = await get_artifacts_by_key(session, key=key)
    assert rows and rows[0].status == "available"
    used_before = None
    async with factory() as session:
        from app.db.repos import get_quota_used
        used_before = await get_quota_used(session, owner_id="u1")

    ok = await delete_artifact_governed(rec=rows[0])
    assert ok is True
    assert not await backend.exists(key)
    async with factory() as session:
        assert await get_artifacts_by_key(session, key=key) == []
        assert await get_quota_used(session, owner_id="u1") == used_before - 12


@pytest.mark.asyncio
async def test_artifact_reconcile_orphan_and_missing(gov):
    """对账（🔴1/🔴5 全状态）：孤儿文件被 GC；missing 记录被补删+冲正。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_artifacts_by_key
    from app.storage import get_backend
    from app.storage.governance import artifact_reconcile_once, record_artifact_meta

    gov.QUOTA_ENABLED = True
    gov.RECONCILE_ENABLED = True
    backend = get_backend()
    # 正常产物（有记录有文件）不受影响
    await backend.put("artifacts/t1/ok.md", b"# ok", mode="overwrite")
    await record_artifact_meta(task_id="t1", rel_path="ok.md", key="artifacts/t1/ok.md",
                               owner_id="u1", size=4, backend="local")
    # 孤儿：存储有、元表无
    await backend.put("artifacts/orphan/x.bin", b"xx", mode="overwrite")
    # missing：元表有、存储无
    await record_artifact_meta(task_id="t2", rel_path="gone.md", key="artifacts/t2/gone.md",
                               owner_id="u1", size=9, backend="local")
    await backend.delete("artifacts/t2/gone.md")

    res = await artifact_reconcile_once(get_session_factory(), backend)
    assert res["orphans"] >= 1 and res["missing"] >= 1
    assert not await backend.exists("artifacts/orphan/x.bin")
    factory = get_session_factory()
    async with factory() as session:
        assert await get_artifacts_by_key(session, key="artifacts/t2/gone.md") == []
    gov.RECONCILE_ENABLED = False


@pytest.mark.asyncio
async def test_init_meta_for_existing_backfill(gov):
    """存量初始化（🔴4）：无元表记录的历史产物补建 + 补配额。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_artifacts_by_key, get_quota_used
    from app.storage import get_backend
    from app.storage.governance import init_meta_for_existing

    gov.QUOTA_ENABLED = True
    gov.ARTIFACT_META_ENABLED = True
    backend = get_backend()
    # 无 meta 环境下先落文件（模拟存量）
    await backend.put("artifacts/t1/legacy.txt", b"legacy-data", mode="overwrite")
    res = await init_meta_for_existing(get_session_factory(), backend)
    assert res.get("inserted", 0) >= 1
    async with get_session_factory()() as session:
        assert await get_artifacts_by_key(session, key="artifacts/t1/legacy.txt")
        assert await get_quota_used(session, owner_id="u1") >= 0  # 无任务→owner None 不计配额


@pytest.mark.asyncio
async def test_version_meta_sync_counts_quota(gov):
    """版本→元表同步（🔴1）：版本归档写入元表并占配额；淘汰后释放。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_quota_used
    from app.storage.governance import record_version_meta, release_version_meta

    gov.QUOTA_ENABLED = True
    akey = "artifacts/_v/t1/doc.md/v1"
    await record_version_meta(task_id="t1", rel_path="doc.md", archive_key=akey,
                              owner_id="u1", size=100, version=1)
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 100
    await release_version_meta(archive_key=akey)
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 0


# ===========================================================================
# 批次 I · 审查闭环：事务预扣 / 结账 / TTL
# ===========================================================================

@pytest.mark.asyncio
async def test_tx_pre_reserve_settle_and_rollback_refund(gov):
    """🔴2/🔴6 事务配额：open 预扣，commit 多退少补，rollback 全额返还。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_quota_used
    from app.storage.governance import tx_commit, tx_open, tx_rollback, tx_stage_write

    gov.QUOTA_ENABLED = True
    gov.QUOTA_TOTAL_MAX_BYTES = 0
    # commit 结账：预扣 50，实际写 10 → 返还 40，净占 10
    info = await tx_open(task_id="tq1", owner_id="u1", estimated_bytes=50)
    tx_id = info["tx_id"]
    await tx_stage_write(tx_id=tx_id, task_id="tq1", rel_path="a.bin", data=b"0123456789")
    r = await tx_commit(tx_id=tx_id)
    assert r["status"] == "committed" and r["bytes"] == 10
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 10
    # rollback 返还：预扣 50，未提交 → 全额返还，净 0
    info2 = await tx_open(task_id="tq2", owner_id="u1", estimated_bytes=50)
    r2 = await tx_rollback(tx_id=info2["tx_id"])
    assert r2["status"] == "rolled_back"
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 10  # 与上一步 commit 的净占一致


@pytest.mark.asyncio
async def test_tx_sweep_expired_rolls_back(gov):
    """🔴3 事务 TTL：超时 pending 事务自动回滚（删暂存 + 返还预扣）。"""
    from datetime import UTC, datetime, timedelta

    import sqlalchemy as sa

    from app.db.base import get_session_factory
    from app.db.models import ArtifactTx
    from app.db.repos import get_quota_used
    from app.storage import get_backend
    from app.storage.governance import tx_open, tx_stage_write, tx_sweep_expired

    gov.QUOTA_ENABLED = True
    gov.TX_ENABLED = True
    backend = get_backend()
    info = await tx_open(task_id="tt1", owner_id="u1", estimated_bytes=20)
    tx_id = info["tx_id"]
    await tx_stage_write(tx_id=tx_id, task_id="tt1", rel_path="s.md", data=b"stage")
    # 回填 created_at 到过去，触发 TTL
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.update(ArtifactTx).where(ArtifactTx.id == tx_id)
            .values(created_at=datetime.now(UTC) - timedelta(seconds=99999)))
        await s.commit()
    rolled = await tx_sweep_expired(get_session_factory(), backend)
    assert rolled == 1
    assert not (backend.root / "artifacts" / "_tx" / tx_id / "tt1" / "s.md").exists()
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 0

# ===========================================================================
# 批次 J · 审查闭环：软删除回收站（deleted 占配额；物理删才释放；restore）
# ===========================================================================

@pytest.mark.asyncio
async def test_soft_delete_keeps_quota_and_restore(gov):
    """🔴5 软删除：deleted 仍占配额；restore 还原。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_artifact_by_rel_status, get_quota_used
    from app.storage.governance import record_artifact_meta, restore_artifact, soft_delete_artifact

    gov.QUOTA_ENABLED = True
    gov.RECYCLE_ENABLED = True
    await record_artifact_meta(task_id="t1", rel_path="r.md", key="artifacts/t1/r.md",
                               owner_id="u1", size=100, backend="local")
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 100
    r = await soft_delete_artifact(task_id="t1", rel_path="r.md")
    assert r["ok"] is True
    # deleted 仍占配额
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 100
        rec = await get_artifact_by_rel_status(session, task_id="t1", rel_path="r.md",
                                               status="deleted")
        assert rec is not None
    rr = await restore_artifact(task_id="t1", rel_path="r.md")
    assert rr["ok"] is True
    async with get_session_factory()() as session:
        rec = await get_artifact_by_rel_status(session, task_id="t1", rel_path="r.md",
                                               status="available")
        assert rec is not None
    gov.RECYCLE_ENABLED = False


@pytest.mark.asyncio
async def test_recycle_sweep_physical_delete_releases_quota(gov):
    """🔴5 回收站过期：物理删文件+元表，才释放配额。"""
    from datetime import UTC, datetime, timedelta

    import sqlalchemy as sa

    from app.db.base import get_session_factory
    from app.db.models import Artifact
    from app.db.repos import get_quota_used
    from app.storage import get_backend
    from app.storage.governance import (
        record_artifact_meta,
        recycle_sweep_expired,
        soft_delete_artifact,
    )

    gov.QUOTA_ENABLED = True
    gov.RECYCLE_ENABLED = True
    gov.ST_RECYCLE_RETENTION_DAYS = 1
    backend = get_backend()
    key = "artifacts/t1/herb.md"
    await backend.put(key, b"# hi", mode="overwrite")
    await record_artifact_meta(task_id="t1", rel_path="herb.md", key=key,
                               owner_id="u1", size=5, backend="local")
    await soft_delete_artifact(task_id="t1", rel_path="herb.md")
    # 回填 deleted_at 到过去，触发回收
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.update(Artifact).where(Artifact.status == "deleted")
            .values(deleted_at=datetime.now(UTC) - timedelta(days=99)))
        await s.commit()
    removed = await recycle_sweep_expired(get_session_factory(), backend)
    assert removed == 1
    assert not await backend.exists(key)  # 物理文件已删
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 0  # 物理删才释放
    gov.RECYCLE_ENABLED = False
    gov.ST_RECYCLE_RETENTION_DAYS = 7


@pytest.mark.asyncio
async def test_reserve_quota_atomic_and_overlimit(gov):
    """原子占位（🔴2/🔴6）：预留不超限成功；超限抛错并回滚减法。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_quota_used
    from app.storage.governance import QuotaExceededError, reserve_quota

    gov.QUOTA_ENABLED = True
    gov.QUOTA_TOTAL_MAX_BYTES = 100
    n1 = await reserve_quota(owner_id="u1", delta=50, limit=100)
    assert n1 == 50
    with pytest.raises(QuotaExceededError):
        await reserve_quota(owner_id="u1", delta=80, limit=100)
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 50  # 超限已回滚
    gov.QUOTA_TOTAL_MAX_BYTES = 0


@pytest.mark.asyncio
async def test_quota_meltdown_on_backfill(gov):
    """熔断（🔴2）：回填后实际超限 → 抛错 + 删文件 + 配额回滚。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_quota_used
    from app.storage import get_backend
    from app.storage.governance import QuotaExceededError, record_artifact_meta

    gov.QUOTA_ENABLED = True
    gov.QUOTA_TOTAL_MAX_BYTES = 50
    backend = get_backend()
    key = "artifacts/tm/a.bin"
    await backend.put(key, b"x" * 100, mode="overwrite")
    with pytest.raises(QuotaExceededError):
        await record_artifact_meta(task_id="tm", rel_path="a.bin", key=key,
                                   owner_id="u1", size=100, backend="local")
    assert not await backend.exists(key)  # 不残留文件
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u1") == 0  # 配额回滚
    gov.QUOTA_TOTAL_MAX_BYTES = 0


@pytest.mark.asyncio
async def test_quota_gray_list_init(gov):
    """灰度（⭐5）：名单非空时，非名单用户不占配额；名单用户占。"""
    from app.db.base import get_session_factory
    from app.db.repos import get_quota_used
    from app.storage.governance import record_artifact_meta

    gov.QUOTA_ENABLED = True
    gov.QUOTA_GRAY_LIST = "u-gold"
    await record_artifact_meta(task_id="tg1", rel_path="a.bin", key="artifacts/tg1/a.bin",
                               owner_id="u-silver", size=10, backend="local")
    await record_artifact_meta(task_id="tg2", rel_path="b.bin", key="artifacts/tg2/b.bin",
                               owner_id="u-gold", size=20, backend="local")
    async with get_session_factory()() as session:
        assert await get_quota_used(session, owner_id="u-silver") == 0
        assert await get_quota_used(session, owner_id="u-gold") == 20
    gov.QUOTA_GRAY_LIST = ""


@pytest.mark.asyncio
async def test_quota_unavailable_gate(gov):
    """就绪门（🔴4）：存量初始化进行中 → check 抛 QuotaUnavailableError。"""
    from app.storage import governance as govm
    from app.storage.governance import QuotaUnavailableError, check_quota

    gov.QUOTA_ENABLED = True
    saved_running = govm._meta_init["running"]
    govm._meta_init["running"] = True
    try:
        with pytest.raises(QuotaUnavailableError):
            await check_quota(owner_id="u1", size=1)
    finally:
        govm._meta_init["running"] = saved_running
