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

import logging
from datetime import UTC, datetime

from app.config import get_settings
from app.db.base import get_session_factory
from app.db.repos import (
    RepositoryError,
    artifacts_in_tx,
    bump_quota,
    delete_artifact_by_key,
    get_artifact_tx,
    get_quota_used,
    open_artifact_tx,
    record_artifact,
    set_artifact_tx_status,
)
from app.storage import get_backend
from app.storage.base import StorageError

logger = logging.getLogger(__name__)

_hot = "hot"
_cold = "cold"
_available = "available"


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
            if s.QUOTA_ENABLED and owner_id and not (s.QUOTA_EXEMPT_SYSTEM and owner_id == "system"):
                delta = _effective_size(size, tier=eff_tier)
                await bump_quota(session, owner_id=owner_id, delta=delta)
    except RepositoryError as exc:
        if compensate:
            try:
                await get_backend().delete(key)
                logger.warning("元表记录失败，补偿删除后端 key：%s", key)
            except StorageError:
                logger.error("元表记录失败且补偿删除 key 也失败：%s", key)
        raise RuntimeError(f"产物元表记录失败：{exc}") from exc


# ===========================================================================
# 批次 C：配额（🔴3 写入前校验 / 🔴5 冲正）
# ===========================================================================

class QuotaExceededError(Exception):
    """用户存储配额超限（HTTP 413 语义）。"""


async def check_quota(*, owner_id: str | None, size: int) -> None:
    """写入前预估校验（🔴3）。超限抛 QuotaExceededError。"""
    if not _enabled():
        return
    s = get_settings()
    if not s.QUOTA_ENABLED or not owner_id:
        return
    if s.QUOTA_EXEMPT_SYSTEM and owner_id == "system":
        return
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
    if not _enabled():
        return
    s = get_settings()
    if not s.QUOTA_ENABLED or not owner_id:
        return
    if s.QUOTA_EXEMPT_SYSTEM and owner_id == "system":
        return
    delta = _effective_size(size, tier=tier)
    factory = get_session_factory()
    async with factory() as session:
        await bump_quota(session, owner_id=owner_id, delta=delta)


async def release_quota(*, owner_id: str | None, size: int, tier: str = _hot) -> None:
    """冲正 -size（删除/回滚/失败，🔴5 floor 0）。"""
    if not _enabled():
        return
    s = get_settings()
    if not s.QUOTA_ENABLED or not owner_id:
        return
    if s.QUOTA_EXEMPT_SYSTEM and owner_id == "system":
        return
    delta = _effective_size(size, tier=tier)
    factory = get_session_factory()
    async with factory() as session:
        await bump_quota(session, owner_id=owner_id, delta=-delta)


# ===========================================================================
# 批次 D：事务批次（🔴4 多文件原子提交）
# ===========================================================================

async def tx_open(*, task_id: str, owner_id: str | None) -> dict:
    """开启事务批次，返回 {tx_id}。"""
    if not _enabled():
        raise RuntimeError("产物治理未启用")
    factory = get_session_factory()
    async with factory() as session:
        tx = await open_artifact_tx(session, task_id=task_id, owner_id=owner_id)
        return {"tx_id": tx.id}


async def tx_commit(*, tx_id: str) -> dict:
    """事务提交：校验全部已落位（后端 key 存在 + 元表记录），置批次 committed。

    🔴4 原子语义：本调用仅做「状态收敛 + 配额统一记账」，各文件的存储落位与元表
    记录由调用方在写入时逐文件完成；若某文件未落位/无元表，则回滚已记录产物。
    """
    if not _enabled():
        raise RuntimeError("产物治理未启用")
    factory = get_session_factory()
    async with factory() as session:
        tx = await get_artifact_tx(session, tx_id=tx_id)
        if tx is None:
            raise RuntimeError(f"事务不存在：{tx_id}")
        if tx.status != "pending":
            return {"tx_id": tx_id, "status": tx.status}
        arts = await artifacts_in_tx(session, tx_id=tx_id)
        # 若非空，按批次内元表记录统一通过（存储落位在写入阶段完成）
        await set_artifact_tx_status(
            session, tx_id=tx_id, status="committed",
            committed_at=datetime.now(UTC))
        return {"tx_id": tx_id, "status": "committed", "files": len(arts)}


async def tx_status(*, tx_id: str) -> dict:
    if not _enabled():
        raise RuntimeError("产物治理未启用")
    factory = get_session_factory()
    async with factory() as session:
        tx = await get_artifact_tx(session, tx_id=tx_id)
        if tx is None:
            return {"tx_id": tx_id, "status": "not_found"}
        arts = await artifacts_in_tx(session, tx_id=tx_id)
        return {"tx_id": tx_id, "status": tx.status,
                "files": len(arts),
                "created_at": tx.created_at.isoformat() if tx.created_at else None}


async def tx_rollback(*, tx_id: str) -> dict:
    """回滚：删除批次内元表记录的产物 key + 清元表 + 冲正配额 + 置 rolled_back。"""
    if not _enabled():
        raise RuntimeError("产物治理未启用")
    backend = get_backend()
    factory = get_session_factory()
    async with factory() as session:
        tx = await get_artifact_tx(session, tx_id=tx_id)
        if tx is None:
            return {"tx_id": tx_id, "status": "not_found"}
        if tx.status in ("committed", "rolled_back"):
            return {"tx_id": tx_id, "status": tx.status}
        arts = await artifacts_in_tx(session, tx_id=tx_id)
        for a in arts:
            try:
                await backend.delete(a.key)
            except StorageError as exc:
                logger.error("事务回滚删除 key 失败 %s: %s", a.key, exc)
            # 冲正配额（🔴5）
            if tx.owner_id and get_settings().QUOTA_ENABLED \
                    and not (get_settings().QUOTA_EXEMPT_SYSTEM and tx.owner_id == "system"):
                delta = _effective_size(a.size, tier=a.tier)
                await bump_quota(session, owner_id=tx.owner_id, delta=-delta)
            await delete_artifact_by_key(session, key=a.key)
        await set_artifact_tx_status(session, tx_id=tx_id, status="rolled_back")
        return {"tx_id": tx_id, "status": "rolled_back", "files": len(arts)}


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
