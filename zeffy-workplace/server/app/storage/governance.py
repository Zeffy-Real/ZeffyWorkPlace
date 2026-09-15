"""P6 产物生命周期治理 · 服务编排层。

聚合存储后端、DB 仓储层、配置于一体，对上层（上传链路 / fs 工具 / 事务 API）提供
无侵入的治理能力。全部能力受 ``ARTIFACT_META_ENABLED`` 总开关控制；关闭时各函数
为安全的 no-op（兼容锚点，P5 行为零漂移）。

能力分三类（对应批次 B/C/D）：
- ``record_artifact_meta``：写入权威元表，失败补偿删后端 key（🔴1/🔴2）。
- 配额：``check_quota`` / ``account_quota`` / ``release_quota``（🔴3/🔴5）。
- 事务：``tx_open`` / ``tx_commit`` / ``tx_rollback``（🔴4）。

注意：本模块不承担后端 put 本身；调用方负责完成存储写入后，再调记账/元表。
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime

from app.config import get_settings
from app.db.base import get_session_factory
from app.db.repos import (
    RepositoryError,
    artifacts_in_tx,
    bump_quota,
    delete_artifact,
    delete_artifact_by_key,
    get_artifact_tx,
    get_quota_used,
    list_tx_staged,
    open_artifact_tx,
    record_artifact,
    set_artifact_tx_reserved,
    set_artifact_tx_status,
    update_artifact_published,
)
from app.storage import get_backend
from app.storage.base import StorageError

logger = logging.getLogger(__name__)

_hot = "hot"
_cold = "cold"
_available = "available"
_pending = "pending"
_deleted = "deleted"
_failed = "failed"

# 存量初始化 / 对账 运行态（进程内，供进度/配额就绪查询）
_meta_init = {"running": False, "done": False, "scanned": 0, "inserted": 0}

# 治理指标计数（并入 /metrics，供配额使用率/分层/审计/熔断/回收监控）
_gov_counters = {
    "audit": 0, "quota_meltdown": 0, "tx_commit": 0, "tx_rollback": 0,
    "recycle_soft": 0, "recycle_restore": 0, "recycle_expired": 0,
    "reconcile_missing": 0, "reconcile_orphan": 0,
}


def governance_metrics() -> dict:
    """治理指标快照（并入 /metrics；进程内计数 + 初始化态）。"""
    return {**dict(_gov_counters), "meta_init": dict(_meta_init)}


def _enabled() -> bool:
    """治理总开关：关闭则全部 no-op（P5 兼容锚点）。"""
    return get_settings().ARTIFACT_META_ENABLED


def _effective_size(size: int, *, tier: str) -> int:
    """配额计量：cold 归档按折算系数降权（可选）。"""
    s = get_settings()
    if tier == _cold and s.TIER_ENABLED:
        factor = max(0.0, s.QUOTA_TIER_COLD_FACTOR)
        return max(0, round(size * factor))
    return size


# ===========================================================================
# 批次 B：权威元表记录（🔴1/🔴2）
# ===========================================================================

async def record_artifact_meta(
    *,
    task_id: str,
    rel_path: str,
    key: str,
    owner_id: str | None,
    size: int,
    backend: str,
    sha256: str = "",
    mime: str = "",
    producer_role: str = "",
    version: int = 0,
    tier: str | None = None,
    status: str = _available,
    tx_id: str | None = None,
    compensate: bool = True,
) -> None:
    """写入权威元表；失败默认补偿删除后端 key（🔴2 防孤儿）。

    总开关关闭时 no-op（元表/配额全跳过）。
    """
    if not _enabled():
        return
    s = get_settings()
    eff_tier = tier or (_hot if not s.TIER_ENABLED else _hot)
    factory = get_session_factory()
    try:
        async with factory() as session:
            await record_artifact(
                session, task_id=task_id, rel_path=rel_path, key=key,
                owner_id=owner_id, size=size, backend=backend,
                sha256=sha256, mime=mime, producer_role=producer_role,
                version=version, status=status, tier=eff_tier, tx_id=tx_id,
            )
            # 配额记账（写入后回填实际占用，🔴3）
            if _quota_enabled_for(owner_id):
                delta = _effective_size(size, tier=eff_tier)
                new_used = await bump_quota(session, owner_id=owner_id, delta=delta)
                # 🔴2 超限熔断：回填后实际超限 → 补偿（保留元表由对账兜底，配额回滚）
                total = get_settings().QUOTA_TOTAL_MAX_BYTES
                if total > 0 and new_used > total:
                    await bump_quota(session, owner_id=owner_id, delta=-delta)
                    await _audit_gov(task_id=task_id, owner_id=owner_id,
                                     action="governance.quota.meltdown",
                                     detail={"key": key, "size": size}, ok=False,
                                     error=f"回填后超限 {new_used}>{total}")
                    _gov_counters["quota_meltdown"] += 1
                    raise QuotaExceededError(
                        f"总配额超限：回填后已用 {new_used} > 上限 {total}") from None
    except RepositoryError as exc:
        if compensate:
            try:
                await get_backend().delete(key)
                logger.warning("元表记录失败，补偿删除后端 key：%s", key)
            except StorageError:
                logger.error("元表记录失败且补偿删除 key 也失败：%s", key)
        raise RuntimeError(f"产物元表记录失败：{exc}") from exc
    except QuotaExceededError:
        # 熔断：删除后端 key 保持不超配（🔴2），不残留文件
        try:
            await get_backend().delete(key)
        except StorageError:
            logger.error("配额熔断删除 key 失败：%s", key)
        raise


# ===========================================================================
# 批次 C：配额（🔴3 写入前校验 / 🔴5 冲正）
# ===========================================================================

class QuotaExceededError(Exception):
    """用户存储配额超限（HTTP 413 语义）。"""


class QuotaUnavailableError(Exception):
    """配额引擎未就绪（存量初始化中，HTTP 503 语义）。"""


def _quota_enabled_for(owner_id: str | None) -> bool:
    """配额是否对该 owner 生效：总开关 + 维度 + system 豁免 + 灰度白名单。"""
    s = get_settings()
    if not (s.QUOTA_ENABLED and owner_id):
        return False
    if s.QUOTA_EXEMPT_SYSTEM and owner_id == "system":
        return False
    gray = [x.strip() for x in (s.QUOTA_GRAY_LIST or "").split(",") if x.strip()]
    if gray and owner_id not in gray:
        return False  # 灰度名单非空且不含该用户 → 该用户配额关
    return True


def quota_ready() -> bool:
    """配额就绪：存量初始化进行中 → False（配额接口应 503）。"""
    if not (_enabled() and get_settings().QUOTA_ENABLED):
        return True
    return not meta_init_state()["running"]


async def reserve_quota(*, owner_id: str | None, delta: int, limit: int = 0) -> int:
    """原子占位（🔴2/🔴6）：先文档原子加 delta；超 limit 回滚减法并抛 QuotaExceeded。

    单条 UPDATE 原子，库行级串行；与 check_quota 的「先查后写」相比消除主竞态窗口。
    返回占位后的用量。
    """
    if not _enabled() or not _quota_enabled_for(owner_id):
        return 0
    if not quota_ready():
        raise QuotaUnavailableError("配额引擎未就绪（存量初始化中）")
    factory = get_session_factory()
    async with factory() as session:
        new_used = await bump_quota(
            session, owner_id=owner_id, delta=delta)  # type: ignore[arg-type]
        if limit and delta > 0 and new_used > limit:
            await bump_quota(session, owner_id=owner_id, delta=-delta)
            raise QuotaExceededError(
                f"总配额超限：已用 {new_used} > 上限 {limit}")
        return new_used


async def check_quota(*, owner_id: str | None, size: int) -> None:
    """写入前预估校验（🔴3）。超限抛 QuotaExceededError。"""
    if not _enabled():
        return
    s = get_settings()
    if not _quota_enabled_for(owner_id):
        return
    if not quota_ready():
        raise QuotaUnavailableError("配额引擎未就绪（存量初始化中）")
    if s.QUOTA_ASSET_MAX_BYTES > 0 and size > s.QUOTA_ASSET_MAX_BYTES:
        raise QuotaExceededError(f"单产物超限：{size} bytes > 上限 {s.QUOTA_ASSET_MAX_BYTES}")
    if s.QUOTA_TOTAL_MAX_BYTES > 0:
        factory = get_session_factory()
        async with factory() as session:
            used = await get_quota_used(session, owner_id=owner_id)
        if used + size > s.QUOTA_TOTAL_MAX_BYTES:
            raise QuotaExceededError(
                f"总配额超限：已用 {used} + 新增 {size} > 上限 {s.QUOTA_TOTAL_MAX_BYTES}")


async def account_quota(*, owner_id: str | None, size: int, tier: str = _hot) -> None:
    """显式记账 +size（供未走 record_artifact 的路径 / 事务 commit）。"""
    if not (_enabled() and _quota_enabled_for(owner_id)):
        return
    delta = _effective_size(size, tier=tier)
    factory = get_session_factory()
    async with factory() as session:
        await bump_quota(session, owner_id=owner_id, delta=delta)  # type: ignore[arg-type]


async def release_quota(*, owner_id: str | None, size: int, tier: str = _hot) -> None:
    """冲正 -size（删除/回滚/失败，🔴5 floor 0）。"""
    if not (_enabled() and _quota_enabled_for(owner_id)):
        return
    delta = _effective_size(size, tier=tier)
    factory = get_session_factory()
    async with factory() as session:
        await bump_quota(session, owner_id=owner_id, delta=-delta)  # type: ignore[arg-type]


# ===========================================================================
# 批次 D：事务批次（🔴4 多文件原子提交 + 可见性隔离）
# ===========================================================================

async def tx_open(*, task_id: str, owner_id: str | None, estimated_bytes: int = 0) -> dict:
    """开启事务批次，返回 {tx_id}（🔴2 预扣配额）。"""
    if not (_enabled() and get_settings().TX_ENABLED):
        raise RuntimeError("事务未启用")
    factory = get_session_factory()
    reserved = 0
    # 预扣配额（estimated 占位；计入事务本身，占配额）
    if estimated_bytes > 0 and _quota_enabled_for(owner_id):
        reserved = await reserve_quota(
            owner_id=owner_id, delta=estimated_bytes,
            limit=get_settings().QUOTA_TOTAL_MAX_BYTES)
    async with factory() as session:
        tx = await open_artifact_tx(session, task_id=task_id, owner_id=owner_id)
        if reserved and tx.id:
            await set_artifact_tx_reserved(session, tx_id=tx.id, reserved=estimated_bytes)
        return {"tx_id": tx.id, "reserved_bytes": estimated_bytes}


async def tx_stage_write(*, tx_id: str, task_id: str, rel_path: str, data,
                         mime: str = "", producer_role: str = "") -> dict:
    """写出临时文件到 ``_tx/{tx_id}/...`` + 记录 pending 元表行（配额由预扣覆盖，不再逐文件记账）。

    🔴4 半提交隔离：pending 行对外查询/列表一律过滤，文件存于暂存空间对外不可读。
    """
    if not (_enabled() and get_settings().TX_ENABLED):
        raise RuntimeError("事务未启用")
    from app.storage.base import tx_staging_key

    payload = bytes(data) if not isinstance(data, bytes) else data
    backend = get_backend()
    skey = tx_staging_key(tx_id, task_id, rel_path)
    meta = await backend.put(skey, payload, mode="overwrite",
                             producer_role=producer_role, mime=mime or None)
    factory = get_session_factory()
    async with factory() as session:
        tx = await get_artifact_tx(session, tx_id=tx_id)
        if tx is None or tx.status != "pending":
            raise RuntimeError(f"事务不存在或未处于 pending：{tx_id}")
        await record_artifact(
            session, task_id=task_id, rel_path=rel_path, key=skey,
            owner_id=tx.owner_id, size=len(payload), backend=backend.name,
            mime=mime, producer_role=producer_role, version=1,
            status=_pending, tier=_hot, tx_id=tx_id)
    return {"key": skey, "size": len(payload), "sha256": meta.sha256}


def _final_from_staging(staging_key: str) -> str:
    """``artifacts/_tx/{tx}/{task}/{rel}`` → ``artifacts/{task}/{rel}``。"""
    rest = staging_key[len("artifacts/_tx/"):]
    _tx_seg, task_id, rel = rest.split("/", 2)
    return f"artifacts/{task_id}/{rel}"


async def tx_commit(*, tx_id: str) -> dict:
    """提交（🔴4 原子可见）：暂存文件写至最终 key（仍不可见）→ 单事务统一置 available+刷新 key+结账。

    可见性由元表状态统一控制：pending 行过滤不可见；最后一步一次 DB 事务翻转为
    available 才对外可见，实现原子发布。极端并发下毫秒级最终一致窗口，可接受。
    """
    if not (_enabled() and get_settings().TX_ENABLED):
        raise RuntimeError("事务未启用")
    backend = get_backend()
    factory = get_session_factory()
    total = 0
    async with factory() as session:
        tx = await get_artifact_tx(session, tx_id=tx_id)
        if tx is None:
            raise RuntimeError(f"事务不存在：{tx_id}")
        if tx.status != "pending":
            return {"tx_id": tx_id, "status": tx.status}
        arts = await list_tx_staged(session, tx_id=tx_id)
    # 阶段1：逐个移动暂存→最终 key（原子）+ 记录刷 key（保持 pending 不可见）
    for a in arts:
        final = _final_from_staging(a.key)
        await backend.move(a.key, final)
        total += a.size
        async with factory() as session:
            await update_artifact_published(session, artifact_id=a.id, key=final,
                                            status=_pending)  # 刷新 key，仍 pending
            await session.commit()
    # 阶段2：单事务原子发布 + 事务终态 + 配额结账（多退少补）
    async with factory() as session:
        for a in arts:
            await update_artifact_published(session, artifact_id=a.id,
                                            key=_final_from_staging(a.key),
                                            status=_available)
        await set_artifact_tx_status(session, tx_id=tx_id, status="committed",
                                     committed_at=datetime.now(UTC))
        if tx.owner_id and _quota_enabled_for(tx.owner_id):
            settle = total - (tx.reserved_bytes or 0)
            if settle:
                await bump_quota(session, owner_id=tx.owner_id, delta=settle)
    await _audit_gov(task_id=tx.task_id or "", owner_id=tx.owner_id or "",
                     action="governance.tx.commit",
                     detail={"tx_id": tx_id, "files": len(arts), "bytes": total})
    _gov_counters["tx_commit"] += 1
    return {"tx_id": tx_id, "status": "committed", "files": len(arts), "bytes": total}


async def tx_status(*, tx_id: str) -> dict:
    if not (_enabled() and get_settings().TX_ENABLED):
        raise RuntimeError("事务未启用")
    factory = get_session_factory()
    async with factory() as session:
        tx = await get_artifact_tx(session, tx_id=tx_id)
        if tx is None:
            return {"tx_id": tx_id, "status": "not_found"}
        arts = await artifacts_in_tx(session, tx_id=tx_id)
        staged = [a for a in arts if a.status == _pending]
        bytes_total = sum(a.size for a in arts if a.key and not a.key.startswith("artifacts/_tx/"))
        return {"tx_id": tx_id, "task_id": tx.task_id or "", "status": tx.status,
                "files": len(arts), "staged": len(staged),
                "reserved_bytes": tx.reserved_bytes or 0,
                "size_bytes": bytes_total,
                "created_at": tx.created_at.isoformat() if tx.created_at else None}


async def tx_rollback(*, tx_id: str) -> dict:
    """回滚（🔴4/🔴6）：删暂存文件 + pending 元表 + 返还预扣配额，幂等无残留。"""
    if not (_enabled() and get_settings().TX_ENABLED):
        raise RuntimeError("事务未启用")
    backend = get_backend()
    factory = get_session_factory()
    async with factory() as session:
        tx = await get_artifact_tx(session, tx_id=tx_id)
        if tx is None:
            return {"tx_id": tx_id, "status": "not_found"}
        if tx.status == "committed":
            return {"tx_id": tx_id, "status": "committed"}
        if tx.status == "rolled_back":
            return {"tx_id": tx_id, "status": "rolled_back"}
        arts = await artifacts_in_tx(session, tx_id=tx_id)
        for a in arts:
            with contextlib.suppress(Exception):
                if a.key.startswith("artifacts/_tx/"):
                    await backend.delete(a.key)
            await delete_artifact(session, artifact_id=a.id)
        await set_artifact_tx_status(session, tx_id=tx_id, status="rolled_back")
        # 🔴2 返还预扣配额
        if tx.owner_id and tx.reserved_bytes and _quota_enabled_for(tx.owner_id):
            await bump_quota(session, owner_id=tx.owner_id, delta=-tx.reserved_bytes)
    await _audit_gov(task_id=tx.task_id or "", owner_id=tx.owner_id or "",
                     action="governance.tx.rollback", detail={"tx_id": tx_id})
    _gov_counters["tx_rollback"] += 1
    return {"tx_id": tx_id, "status": "rolled_back", "files": len(arts)}


async def tx_sweep_expired(session_factory, backend) -> int:
    """🔴3 事务 TTL 守护：超时 pending 事务自动回滚（删暂存 + 元表 + 返还预扣）。"""
    if not (_enabled() and get_settings().TX_ENABLED):
        return 0
    from datetime import UTC, timedelta

    from sqlalchemy import select

    from app.db.models import ArtifactTx

    s = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=max(60, s.TX_TTL))
    rolled = 0
    try:
        async with session_factory() as session:
            rows = (await session.execute(
                select(ArtifactTx).where(ArtifactTx.status == "pending",
                                         ArtifactTx.created_at < cutoff)
            )).scalars().all()
        for tx in rows:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await tx_rollback(tx_id=tx.id)
                rolled += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("事务 TTL 巡检失败：%s", exc)
    return rolled


# ===========================================================================
# 批次 E：存储分层（元数据标记 + 真实归档）
# ===========================================================================

async def tier_archive(*, task_id: str, rel_path: str) -> dict:
    """把某产物降为 cold 归档：后端真实归档 + 更新元表 tier 字段。

    幂等：已 cold 再调视为成功。治理/分层关则 no-op。
    """
    if not _enabled():
        return {"ok": False, "reason": "disabled"}
    s = get_settings()
    if not s.TIER_ENABLED:
        return {"ok": False, "reason": "tier_disabled"}
    backend = get_backend()
    factory = get_session_factory()
    from app.db.repos import get_artifact_by_rel, update_artifact_tier

    async with factory() as session:
        rec = await get_artifact_by_rel(session, task_id=task_id, rel_path=rel_path)
        if rec is None:
            return {"ok": False, "reason": "not_found"}
        if rec.tier == _cold:
            return {"ok": True, "tier": _cold}
        archived = await backend.archive_cold(rec.key)
        if not archived:
            return {"ok": False, "reason": "archive_failed"}
        await update_artifact_tier(session, artifact_id=rec.id, tier=_cold)
    return {"ok": True, "tier": _cold}


async def cold_sweep_once(session_factory, backend) -> int:
    """守护冷化：扫描 available+hot 且超出冷化年龄的产物 → 归档。返回归档数。"""
    if not _enabled():
        return 0
    s = get_settings()
    if not s.TIER_ENABLED:
        return 0
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.db.models import Artifact
    from app.db.repos import update_artifact_tier

    cutoff = datetime.now(UTC) - timedelta(seconds=max(60, s.TIER_COLD_ARCHIVE_AGE))
    archived = 0
    try:
        async with session_factory() as session:
            rows = (await session.execute(
                select(Artifact).where(
                    Artifact.status == _available,
                    Artifact.tier == _hot,
                    Artifact.created_at < cutoff,
                )
            )).scalars().all()
            for rec in rows:
                try:
                    ok = await backend.archive_cold(rec.key)
                    if ok:
                        await update_artifact_tier(session, artifact_id=rec.id, tier=_cold)
                        archived += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("冷化失败 %s: %s", rec.key, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("冷化扫描失败：%s", exc)
    return archived


# ===========================================================================
# 批次 G · 一致性兜底 / 对账守护 / 存量初始化 / 版本→治理同步（🔴1/🔴4/🔴5）
# ===========================================================================

async def _audit_gov(*, task_id: str = "", owner_id: str = "", action: str,
                     detail: dict | None = None, ok: bool = True, error: str = "") -> None:
    """治理全链路审计（批 L 统一入口；关闭态零开销）。"""
    s = get_settings()
    if not (_enabled() and s.AUDIT_GOVERNANCE_ENABLED):
        return
    _gov_counters["audit"] += 1
    from app.db import repos

    factory = get_session_factory()
    try:
        async with factory() as session:
            await repos.write_audit(
                session, task_id=task_id or "", operator=owner_id or "system",
                action=action, detail={**(detail or {}), "ok": ok, "error": error})
    except Exception:  # noqa: BLE001
        logger.exception("治理审计写入失败 action=%s", action)


async def delete_artifact_governed(*, rec, backend=None, reason: str = "") -> bool:
    """统一物理删除编排（🔴5 删除顺序唯一入口）：先删存储 → 删元表 → 冲正配额。

    每步失败重试一次；存储删失败则保留元表（交由对账兜底），并审计。
    ``rec`` 为 Artifact 元记录。返回是否物理删成功。
    """
    backend = backend or get_backend()
    key = rec.key
    # 1) 删存储（重试一次）
    try:
        deleted = await backend.delete(key)
        if not deleted:
            deleted = await backend.delete(key)
    except StorageError as exc:
        await _audit_gov(task_id=rec.task_id or "", owner_id=rec.owner_id or "",
                         action="governance.delete.storage_fail",
                         detail={"key": key, "reason": reason}, ok=False, error=str(exc))
        logger.error("物理删除存储失败 key=%s: %s", key, exc)
        return False
    # 2) 删元表
    factory = get_session_factory()
    async with factory() as session:
        await delete_artifact_by_key(session, key=key)
        # 3) 冲正配额（effective，floor 0）
        if rec.owner_id and get_settings().QUOTA_ENABLED \
                and not (get_settings().QUOTA_EXEMPT_SYSTEM and rec.owner_id == "system"):
            delta = _effective_size(rec.size, tier=rec.tier)
            await bump_quota(session, owner_id=rec.owner_id, delta=-delta)
    await _audit_gov(task_id=rec.task_id or "", owner_id=rec.owner_id or "",
                     action="governance.delete", detail={"key": key, "reason": reason})
    return True


async def _list_governance_keys(backend) -> list[str]:
    """存储正式产物 key 全集（排除 _tx/_tmp/_v 临时/版本空间）。"""
    try:
        keys = await backend.list("artifacts/")
    except StorageError:
        return []
    out = []
    for k in keys:
        if k.startswith("artifacts/_tx/") or k.startswith("artifacts/_tmp/") \
                or k.startswith("artifacts/_v/"):
            continue
        out.append(k)
    return out


async def artifact_reconcile_once(session_factory, backend) -> dict:
    """存储与元表对账（🔴1/🔴5 全状态）：DB 无文件→missing 补删记录+冲正；文件无记录→orphan GC。"""
    s = get_settings()
    if not (_enabled() and s.RECONCILE_ENABLED):
        return {"enabled": False}
    from app.db import repos

    res = {"scanned": 0, "missing": 0, "orphans": 0, "removed_rows": 0, "removed_files": 0}
    # 1) DB key 集合
    db_keys: dict[str, list] = {}
    async with session_factory() as session:
        rows = await repos.list_all_artifacts(session, limit=100000)
        for r in rows:
            if r.key:
                db_keys.setdefault(r.key, []).append(r)
    # 2) 存储正式 key 集合
    store_keys = await _list_governance_keys(backend)
    store_set = set(store_keys)
    # 3) orphan：存储有、DB 无 → 清文件（可选）
    for k in store_keys:
        if k not in db_keys:
            res["orphans"] += 1
            _gov_counters["reconcile_orphan"] += 1
            if s.RECONCILE_ORPHAN_GC:
                try:
                    await backend.delete(k)
                    res["removed_files"] += 1
                    await _audit_gov(action="governance.reconcile.orphan_gc",
                                     detail={"key": k})
                except StorageError:
                    logger.error("对账清孤儿失败 key=%s", k)
    # 4) missing：DB 有、存储无 → 补删记录 + 冲正配额
    for key, recs in db_keys.items():
        res["scanned"] += 1
        if key in store_set:
            continue
        if not await backend.exists(key):
            res["missing"] += 1
            _gov_counters["reconcile_missing"] += 1
            async with session_factory() as session:
                for a in recs:
                    if a.owner_id and s.QUOTA_ENABLED \
                            and not (s.QUOTA_EXEMPT_SYSTEM and a.owner_id == "system"):
                        await repos.bump_quota(
                            session, owner_id=a.owner_id,
                            delta=-_effective_size(a.size, tier=a.tier))
                    await repos.delete_artifact(session, artifact_id=a.id)
                    res["removed_rows"] += 1
            await _audit_gov(action="governance.reconcile.missing_gc",
                             detail={"key": key, "count": len(recs)})
    return res


def meta_init_state() -> dict:
    """存量初始化进度/就绪状态（配额就绪判定用）。"""
    return dict(_meta_init)


async def init_meta_for_existing(session_factory, backend) -> dict:
    """存量初始化（🔴4）：首次启用且元表空时，后台扫描正式产物补建元表+补齐配额。

    返回 {scanned, inserted, skipped}；分配 owner 取任务归属（task 已删则 None，不计配额）。
    """
    if not _enabled() or _meta_init["running"]:
        return {"scanned": _meta_init["scanned"], "inserted": _meta_init["inserted"],
                "running": _meta_init["running"]}
    from app.db import repos

    # 若已有记录 → 视为已初始化，直接标记 done
    async with session_factory() as session:
        existing = await repos.list_all_artifacts(session, limit=1)
        if existing:
            _meta_init.update({"done": True, "running": False})
            return {"scanned": 0, "inserted": 0, "running": False, "skipped": "already_initialized"}

    _meta_init["running"] = True
    keys = await _list_governance_keys(backend)
    inserted = 0
    try:
        for key in keys:
            _meta_init["scanned"] += 1
            async with session_factory() as session:
                recs = await repos.get_artifacts_by_key(session, key=key)
                if recs:
                    continue
                size = await backend.size(key) or 0
                task_id = key.split("/")[1] if len(key.split("/")) > 1 else ""
                owner_id = None
                if task_id:
                    owner_id = await repos.get_owner_or_none(session, task_id)
                rel_path = "/".join(key.split("/")[2:]) or ""
                await repos.record_artifact(
                    session, task_id=task_id or None, rel_path=rel_path, key=key,
                    owner_id=owner_id, size=size, backend=backend.name,
                    status=_available, tier=_hot)
                if owner_id and get_settings().QUOTA_ENABLED \
                        and not (get_settings().QUOTA_EXEMPT_SYSTEM and owner_id == "system"):
                    await repos.bump_quota(
                        session, owner_id=owner_id, delta=_effective_size(size, tier=_hot))
                inserted += 1
    finally:
        _meta_init.update({"running": False, "done": True, "inserted": inserted})
    return {"scanned": _meta_init["scanned"], "inserted": inserted,
            "running": False, "done": True}


async def record_version_meta(*, task_id: str, rel_path: str, archive_key: str,
                              owner_id: str | None, size: int, sha256: str = "",
                              mime: str = "", version: int) -> None:
    """版本→元表同步（🔴1 版本纳入元表/配额）：归档版本 available 时记录一条 artifacts 行。"""
    if not _enabled():
        return
    from app.db import repos

    async with get_session_factory()() as session:  # noqa: E402  (会话资源)
        await repos.record_artifact(
            session, task_id=task_id, rel_path=rel_path, key=archive_key,
            owner_id=owner_id, size=size, backend=get_backend().name,
            sha256=sha256, mime=mime, version=version, status=_available, tier=_hot)
        if owner_id and get_settings().QUOTA_ENABLED \
                and not (get_settings().QUOTA_EXEMPT_SYSTEM and owner_id == "system"):
            await repos.bump_quota(
                session, owner_id=owner_id, delta=_effective_size(size, tier=_hot))


async def release_version_meta(*, archive_key: str) -> None:
    """版本→元表同步：版本淘汰/删除时删除该版本元表行 + 冲正配额。"""
    if not _enabled():
        return
    from app.db import repos

    async with get_session_factory()() as session:
        recs = await repos.get_artifacts_by_key(session, key=archive_key)
        for a in recs:
            if a.owner_id and get_settings().QUOTA_ENABLED \
                    and not (get_settings().QUOTA_EXEMPT_SYSTEM and a.owner_id == "system"):
                await repos.bump_quota(
                    session, owner_id=a.owner_id,
                    delta=-_effective_size(a.size, tier=a.tier))
            await repos.delete_artifact(session, artifact_id=a.id)


# ===========================================================================
# 批次 J · 软删除回收站（🔴5：deleted 继续占配额；物理删才释放；restore 前校验）
# ===========================================================================

async def soft_delete_artifact(*, task_id: str, rel_path: str, reason: str = "") -> dict:
    """软删：status→deleted + deleted_at。🔴5 删除态仍占配额（回收站），仅物理删才释放。"""
    if not (_enabled() and get_settings().RECYCLE_ENABLED):
        return {"ok": False, "reason": "disabled"}
    from app.db import repos

    factory = get_session_factory()
    async with factory() as session:
        rec = await repos.get_artifact_by_rel(session, task_id=task_id, rel_path=rel_path)
        if rec is None:
            return {"ok": False, "reason": "not_found"}
        if not await repos.set_artifact_deleted(session, artifact_id=rec.id):
            return {"ok": False, "reason": "noop"}
    await _audit_gov(task_id=task_id, owner_id=rec.owner_id or "",
                     action="governance.recycle.soft_delete",
                     detail={"rel_path": rel_path, "reason": reason})
    _gov_counters["recycle_soft"] += 1
    return {"ok": True, "status": "deleted"}


async def restore_artifact(*, task_id: str, rel_path: str) -> dict:
    """软删恢复：status→available + 清 deleted_at。deleted 态已占配额，恢复不改变配额；
    恢复前仍校验配额（可用容量充足），不足则释放失败（🔴3 修）。"""
    if not (_enabled() and get_settings().RECYCLE_ENABLED):
        return {"ok": False, "reason": "disabled"}
    from app.db import repos

    factory = get_session_factory()
    async with factory() as session:
        rec = await repos.get_artifact_by_rel_status(
            session, task_id=task_id, rel_path=rel_path, status=_deleted)
        if rec is None:
            return {"ok": False, "reason": "not_found_in_recycle"}
        total = get_settings().QUOTA_TOTAL_MAX_BYTES
        if _quota_enabled_for(rec.owner_id) and total > 0:
            used = await repos.get_quota_used(session, owner_id=rec.owner_id)
            if used + _effective_size(rec.size, tier=rec.tier) > total:
                return {"ok": False, "reason": "quota_full"}
        if not await repos.restore_artifact(session, artifact_id=rec.id):
            return {"ok": False, "reason": "noop"}
    await _audit_gov(task_id=task_id, owner_id=rec.owner_id or "",
                     action="governance.recycle.restore",
                     detail={"rel_path": rel_path})
    _gov_counters["recycle_restore"] += 1
    return {"ok": True, "status": "available"}


async def list_recycle(*, owner_id: str | None, page: int = 1, page_size: int = 50) -> dict:
    """回收站列表（status=deleted，按 deleted_at 排序）。"""
    if not (_enabled() and get_settings().RECYCLE_ENABLED):
        return {"items": [], "total": 0}
    from sqlalchemy import func, select

    from app.db.models import Artifact

    factory = get_session_factory()
    async with factory() as session:
        cond = [Artifact.status == _deleted]
        if owner_id:
            cond.append(Artifact.owner_id == owner_id)
        total = await session.scalar(
            select(func.count()).select_from(Artifact).where(*cond))
        rows = (await session.execute(
            select(Artifact).where(*cond)
            .order_by(Artifact.deleted_at.asc())
            .offset((max(1, page) - 1) * page_size).limit(page_size)
        )).scalars().all()
        items = [{
            "id": a.id, "task_id": a.task_id, "rel_path": a.rel_path,
            "size": a.size, "tier": a.tier, "key": a.key,
            "deleted_at": a.deleted_at.isoformat() if a.deleted_at else None,
        } for a in rows]
    return {"items": items, "total": int(total or 0)}


async def recycle_sweep_expired(session_factory, backend) -> int:
    """🔴5 GC：物理删除超 ST_RECYCLE_RETENTION_DAYS 的 deleted 记录（删文件+元表+释放配额）。"""
    if not (_enabled() and get_settings().RECYCLE_ENABLED):
        return 0
    from datetime import UTC, datetime, timedelta

    from app.db import repos

    s = get_settings()
    cutoff = datetime.now(UTC) - timedelta(
        days=max(1, s.ST_RECYCLE_RETENTION_DAYS))
    removed = 0
    try:
        async with session_factory() as session:
            stale = await repos.stale_deleted_artifacts(session, older_than=cutoff, limit=200)
        for rec in stale:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await delete_artifact_governed(rec=rec, backend=backend,
                                               reason="recycle_expired")
                _gov_counters["recycle_expired"] += 1
                removed += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("回收站过期清理失败：%s", exc)
    return removed
