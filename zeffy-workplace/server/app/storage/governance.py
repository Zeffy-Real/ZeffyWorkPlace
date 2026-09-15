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
from functools import wraps
from typing import TypeGuard

from app.config import get_settings
from app.db import repos
from app.db.base import get_session_factory
from app.db.repos import (
    RepositoryError,
    artifacts_in_tx,
    bump_quota,
    delete_artifact,
    get_artifact_tx,
    get_quota_used,
    list_tx_staged,
    open_artifact_tx,
    record_artifact,
    set_artifact_tx_reserved,
    set_artifact_tx_status,
    update_artifact_published,
    update_artifact_status,
)
from app.storage import get_backend
from app.storage.base import StorageError

logger = logging.getLogger(__name__)

_hot = "hot"
_warm = "warm"
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
    "reconcile_missing": 0, "reconcile_orphan": 0, "gray_hits": 0,
}


# 事务失败分类计数（E-3：tx_fail_by_type 维度，A-1 可定位）
_gov_tx_fail_by_type = {"user": 0, "ttl": 0, "error": 0}


def governance_metrics() -> dict:
    """治理指标快照（并入 /metrics；进程内计数 + 初始化态）。

    E-3：额外输出 ``reconcile_by_type``（missing/orphan 分类）与 ``tx_fail_by_type``
    （按失败来源用户/超时/异常），配合全局聚合做可定位维度。
    """
    m = dict(_gov_counters)
    m["reconcile_by_type"] = {
        "missing": m.get("reconcile_missing", 0),
        "orphan": m.get("reconcile_orphan", 0),
    }
    m["tx_fail_by_type"] = dict(_gov_tx_fail_by_type)
    return {**m, "meta_init": dict(_meta_init)}


# ---- P6-4 E-2 守护状态（运维面板 D-3：运行态/上次运行时间/结果/错误，脱敏）----
_guardian: dict[str, dict] = {}


def _guardian_ping(name: str) -> None:
    _guardian[name] = {"ts": datetime.now(UTC).isoformat(), "running": True,
                       "ok": None, "result": None, "error": ""}


def guardian_status() -> dict:
    """守护任务状态快照（运维面板；仅结构内字段，不含密钥/路径等敏感）。"""
    return {k: dict(v) for k, v in _guardian.items()}


def guardian(name: str):
    """守护装饰器：记录该守护单次运行的 时间/成败/结果/错误（进程内，供面板展示）。

    - 不影响原函数行为（仅透明旁路记录）；
    - 治理总开关关闭时原函数仍 no-op（保留此记录，便于定位「为什么未执行」）。
    """

    def _wrap(fn):
        @wraps(fn)
        async def _inner(*args, **kwargs):
            _guardian_ping(name)
            try:
                res = await fn(*args, **kwargs)
                _guardian[name].update({"running": False, "ok": True, "result": res})
                return res
            except Exception as exc:  # noqa: BLE001
                _guardian[name].update({"running": False, "ok": False,
                                        "error": str(exc)})
                raise

        return _inner

    return _wrap


def _enabled() -> bool:
    """治理总开关：关闭则全部 no-op（P5 兼容锚点）。运行时覆盖优先（D 应急回滚）。"""
    return _ovr.get("meta", get_settings().ARTIFACT_META_ENABLED)


# ---- P6-4 D 运行时覆盖（仅进程内，重启恢复配置默认；供应急回滚/单功能开关）----
_ovr: dict[str, bool] = {}


def _feat_on(name: str, default: bool) -> bool:
    return _ovr.get(name, default)


def set_governance_override(name: str, enabled: bool) -> None:
    """设运行时覆盖（不持久化）。"""
    if enabled:
        _ovr[name] = True
    else:
        _ovr[name] = False


def _ovr_get(name: str) -> bool | None:
    """运行时覆盖值；未覆盖返回 None。"""
    return _ovr.get(name)


# ---- P6-4 C 灰度名单（按「功能 + 用户维度」动态门控；进程内单实例）----
# 注意：与 override/配置为「进程内」态，多实例部署下各实例不同步 → 仅适用于单实例；
#       需中心化时下沉 Redis（见 P6-4 评审 E-4 单实例标注）。
_gray: dict[str, set[str]] = {}


def gray_enabled(feature: str, owner_id: str | None) -> bool:
    """灰度判定核心：**运行时覆盖 > 灰度名单 > 配置默认**。

    - 运行时覆盖存在 → 直接生效（覆盖灰度名单，即应急回滚可无视灰度强制关/开）；
    - 否则若该功能配了非空灰度名单 → 门控：仅名单内 owner 命中；
    - 未配灰度名单 → 放行，回落到配置默认（由调用方用 ``_feat_on``/配置判定）。
    """
    ovr = _ovr.get(feature)
    if ovr is not None:
        return bool(ovr)
    gate = _gray.get(feature)
    if gate:
        return owner_id in gate
    return True


def gray_set(feature: str, add: list[str], remove: list[str]) -> dict:
    """增/删灰度名单。名单增删后若为空则删除该功能键（=灰度关闭，全量放行）。"""
    gs = _gray.setdefault(feature, set())
    before = sorted(gs)
    gs.update(add)
    for o in remove:
        gs.discard(o)
    if not gs:
        _gray.pop(feature, None)
    return {"before": before, "members": sorted(gs)}


def gray_members() -> dict[str, list[str]]:
    """灰度名单快照（运维面板用）。"""
    return {k: sorted(v) for k, v in _gray.items()}


def governance_status() -> dict:
    """运维面板状态：各功能 有效值(=覆盖/灰度或配置默认) + 覆盖标记 + 灰度名单（脱敏，不含敏感）。"""
    s = get_settings()
    f = {
        "meta": (s.ARTIFACT_META_ENABLED, "ARTIFACT_META_ENABLED"),
        "quota": (s.QUOTA_ENABLED, "QUOTA_ENABLED"),
        "dedup": (s.DEDUP_ENABLED, "DEDUP_ENABLED"),
        "quota_history": (s.QUOTA_HISTORY_ENABLED, "QUOTA_HISTORY_ENABLED"),
        "tier": (s.TIER_ENABLED, "TIER_ENABLED"),
        "tx": (s.TX_ENABLED, "TX_ENABLED"),
        "recycle": (s.RECYCLE_ENABLED, "RECYCLE_ENABLED"),
        "reconcile": (s.RECONCILE_ENABLED, "RECONCILE_ENABLED"),
        "audit": (s.AUDIT_GOVERNANCE_ENABLED, "AUDIT_GOVERNANCE_ENABLED"),
    }
    grays = gray_members()
    return {
        "features": [
            {"name": k, "config_attr": attr, "config_default": d,
             "effective": _ovr.get(k, d), "overridden": k in _ovr,
             "gray_gated": k in grays, "gray_members": grays.get(k, [])}
            for k, (d, attr) in f.items()
        ],
        "guardians": guardian_status(),  # 守护任务 运行态/上次运行时间/结果/错误（脱敏）
        "gray": grays,  # 按功能 -> 灰度 owner 列表
        "single_instance_only": True,  # 愿覆盖/灰度仅进程内，多实例需中心化
        "ts": datetime.now(UTC).isoformat(),
    }


def _effective_size(size: int, *, tier: str) -> int:
    """配额计量：cold 归档按折算系数降权（可选）。"""
    s = get_settings()
    if tier == _cold and s.TIER_ENABLED:
        factor = max(0.0, s.QUOTA_TIER_COLD_FACTOR)
        return max(0, round(size * factor))
    return size


# ===========================================================================
# P6-2 O4 · 内容寻址去重（占坑优先 / 回滚 / 命名空间 gate，🔴全局-1/3）
# ===========================================================================

def _dedup_enabled() -> bool:
    return _enabled() and _feat_on("dedup", get_settings().DEDUP_ENABLED)


def dedup_eligible(*, rel_path: str, size: int, mime: str) -> bool:
    """G3 命名空间白名单：仅正式产物、>=最小大小、非排除类型、任务前缀命中才去重。"""
    s = get_settings()
    if not _dedup_enabled():
        return False
    if size < s.DEDUP_MIN_SIZE:
        return False
    if rel_path.startswith(("_tx/", "_tmp/", "_upload/", "_v/", "_dedup/")):
        return False
    if s.DEDUP_EXCLUDE_TYPES and mime in [x.strip().lower() for x in s.DEDUP_EXCLUDE_TYPES.split(",") if x.strip()]:
        return False
    if s.DEDUP_NAMESPACE_TASKS:
        allow = [x.strip() for x in s.DEDUP_NAMESPACE_TASKS.split(",") if x.strip()]
        tid = rel_path.split("/", 1)[0] if "/" in rel_path else ""
        if tid and not any(tid.startswith(p) for p in allow):
            return False
    return True


async def dedup_claim(*, sha256: str, size: int, backend=None) -> tuple[str | None, bool]:
    """G1 占坑优先：``content_upsert`` 原子占坑，返回 ``(物理key, is_dedup)``。

    - is_dedup=True 且返回非 None → 去重路径可用；``is_first`` 语义由调用方判断（需二次确认）；
    - 返回 ``(None, False)`` 表示哈希碰撞（同 sha 但 size 不一致）→ 调用方走普通逐文件路径，不参与去重。
    """
    from app.db import repos as repos_mod
    from app.storage.base import dedup_key

    async with get_session_factory()() as session:
        refs = await repos_mod.content_upsert(session, sha256=sha256, size=size)
    is_first = (refs == 1)
    if not is_first and get_settings().DEDUP_COLLISION_CHECK:
        # 双重校验：复用前比对 size，防止 sha 碰撞误合并不同文件
        async with get_session_factory()() as session:
            c = await repos_mod.content_get(session, sha256=sha256)
        if c is not None and c.size != size:
            await dedup_abort(sha256=sha256)
            return None, False  # 碰撞 → 不参与去重
    return dedup_key(sha256), is_first


async def dedup_abort(*, sha256: str, backend=None) -> None:
    """占坑后写入失败回滚：``content_release``；refs 归 0 → 无引用，清理物理残留。"""
    from app.db import repos as repos_mod
    from app.storage.base import dedup_key

    async with get_session_factory()() as session:
        refs = await repos_mod.content_release(session, sha256=sha256)
    if refs <= 0:
        backend = backend or get_backend()
        try:
            await backend.delete(dedup_key(sha256))
        except StorageError:
            logger.warning("去重回滚清理残留失败 sha=%s", sha256[:8])


@guardian("dedup_backfill")
async def dedup_backfill_once(session_factory, redis=None) -> dict:
    """O4-F 存量去重：扫描未去重(available, content_ref 空, >=MIN)的正式产物，
    流式哈希 → 占坑 → 迁移物理到内容寻址 key / 复用 → 切链 content_ref → 删旧副本。

    受全局锁保护（实时/存量去重互斥，🔴O4-F-2）；分批 + sleep 限速，低峰由 _gc_loop 调度。
    """
    s = get_settings()
    if not _dedup_enabled():
        return {"enabled": False}
    if redis is not None:
        try:
            got = await redis.set("artifacts:dedup:backfill_lock", s.worker_id,
                                  nx=True, ex=max(300, s.DEDUP_BACKFILL_INTERVAL))
        except Exception:  # noqa: BLE001
            got = True
        if not got:
            return {"locked_out": 1, "scanned": 0, "merged": 0}
    from sqlalchemy import select

    from app.db.models import Artifact
    from app.storage.base import dedup_key

    res = {"scanned": 0, "merged": 0, "collision": 0}
    candidate: list[Artifact] = []
    async with session_factory() as session:
        rows = (await session.execute(
            select(Artifact).where(
                Artifact.status == _available,
                Artifact.content_ref.is_(None),
                Artifact.size >= s.DEDUP_MIN_SIZE,
            ).order_by(Artifact.id).limit(s.DEDUP_BACKFILL_BATCH)
        )).scalars().all()
        candidate = list(rows)
    for rec in candidate:
        if not dedup_eligible(rel_path=rec.rel_path or "", size=rec.size,
                              mime=rec.mime or ""):
            continue
        res["scanned"] += 1
        try:
            sha = await _stream_sha256(get_backend(), rec.key)
            pkey, is_first = await dedup_claim(sha256=sha, size=rec.size,
                                               backend=get_backend())
            if pkey is None:
                res["collision"] += 1
                continue
            dkey = dedup_key(sha)
            old = rec.key
            if is_first and old != dkey:
                await get_backend().move(old, dkey)  # 该文件成为唯一内容源
            elif not is_first and old != dkey:
                await get_backend().delete(old)  # 重复副本 → 删除
            async with session_factory() as session:
                await repos.update_artifact_content_ref(
                    session, artifact_id=rec.id, key=dkey, content_ref=sha)
            res["merged"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("存量去重失败 rec=%s: %s", rec.id, exc)
    return res


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
    content_ref: str | None = None,
) -> None:
    """写入权威元表；失败默认补偿（🔴2 防孤儿）。

    - 非去重（content_ref=None）→ 补偿删除后端 key；
    - 去重（content_ref 有值）→ 补偿走 ``dedup_abort``（content_release，refs 归 0 才删物理），
      避免误删共享物理文件（G2 双向补偿）。
    总开关关闭时 no-op（元表/配额全跳过）。
    """

    async def _compensate() -> None:
        if content_ref:
            await dedup_abort(sha256=content_ref)
        else:
            try:
                await get_backend().delete(key)
                logger.warning("元表记录失败，补偿删除后端 key：%s", key)
            except StorageError:
                logger.error("元表记录失败且补偿删除 key 也失败：%s", key)

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
                content_ref=content_ref,
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
            await _compensate()
        raise RuntimeError(f"产物元表记录失败：{exc}") from exc
    except QuotaExceededError:
        # 熔断：补偿（去重→content_release；非去重→删 key）保持不超配（🔴2）
        await _compensate()
        raise


# ===========================================================================
# 批次 C：配额（🔴3 写入前校验 / 🔴5 冲正）
# ===========================================================================

class QuotaExceededError(Exception):
    """用户存储配额超限（HTTP 413 语义）。"""


class QuotaUnavailableError(Exception):
    """配额引擎未就绪（存量初始化中，HTTP 503 语义）。"""


def _quota_enabled_for(owner_id: str | None) -> TypeGuard[str]:
    """配额是否对该 owner 生效：总开关 + 维度 + system 豁免 + 灰度门控。

    决议优先级：**运行时覆盖 > 运行时灰度名单 > 配置默认(+静态 QUOTA_GRAY_LIST)**。
    返回 True 时保证 owner_id 非空（TypeGuard 让调用方在分支内收窄为 str）。
    """
    if not owner_id:
        return False
    s = get_settings()
    if s.QUOTA_EXEMPT_SYSTEM and owner_id == "system":
        return False
    # ① 运行时覆盖：显式 false → 全局关（无视灰度，应急回滚语义）
    if _ovr_get("quota") is False:
        return False
    # ② 运行时灰度名单：非空名单 → 门控（命中才走；查不到名单 → 放行到配置）
    rgate = _gray.get("quota")
    if rgate:
        if owner_id not in rgate:
            return False
        _gov_counters["gray_hits"] += 1
        # 命中灰度 → 以覆盖(true)或配置默认起效；覆盖 false 已在上方拦截
        return bool(_ovr_get("quota")) or bool(s.QUOTA_ENABLED)
    # ③ 配置默认 + 静态灰度名单
    if not s.QUOTA_ENABLED:
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
    P6-2 O4：事务内文件写 _tx 不参与去重；commit 统一流式哈希 + 占坑合并（is_first 才落盘）。
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
        content_ref = None
        if _dedup_enabled() and a.size >= get_settings().DEDUP_MIN_SIZE \
                and dedup_eligible(rel_path=a.rel_path or "", size=a.size, mime=a.mime or ""):
            sha = await _stream_sha256(backend, a.key)
            pkey, is_first = await dedup_claim(sha256=sha, size=a.size, backend=backend)
            if pkey is not None:
                if is_first:
                    await backend.move(a.key, pkey)  # 首引：暂存→内容寻址物理
                else:
                    await backend.delete(a.key)  # 复用：丢弃暂存副本
                final, content_ref = pkey, sha
            else:
                await backend.move(a.key, final)  # 碰撞 → 逐文件
        else:
            await backend.move(a.key, final)
        total += a.size
        async with factory() as session:
            await update_artifact_published(session, artifact_id=a.id, key=final,
                                            status=_pending, content_ref=content_ref)
            await session.commit()
    # 阶段2：单事务原子发布 + 事务终态 + 配额结账（多退少补）
    async with factory() as session:
        for a in arts:
            # 阶段1 已把 key/content_ref 刷成最终值；此处仅翻转状态 available（不重置 key）
            await update_artifact_status(session, artifact_id=a.id, status=_available)
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


async def _stream_sha256(backend, key: str) -> str:
    """流式计算存储 key 文件 sha256（不整文件入内存，🔴O4-B-1）。"""
    import hashlib

    h = hashlib.sha256()
    try:
        async for chunk in backend.stream(key):
            h.update(chunk)
    except StorageError:
        raise
    return h.hexdigest()


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


async def tx_rollback(*, tx_id: str, source: str = "user") -> dict:
    """回滚（🔴4/🔴6）：删暂存文件 + pending 元表 + 返还预扣配额，幂等无残留。

    ``source`` 记录回滚来源（user=用户显式 / ttl=事务超时守护），并入 tx_fail_by_type 维度。
    """
    if not (_enabled() and get_settings().TX_ENABLED):
        raise RuntimeError("事务未启用")
    backend = get_backend()
    factory = get_session_factory()
    try:
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
        _gov_tx_fail_by_type[source] = _gov_tx_fail_by_type.get(source, 0) + 1
        return {"tx_id": tx_id, "status": "rolled_back", "files": len(arts)}
    except Exception:  # noqa: BLE001 回滚本身异常 → 归类 error
        _gov_tx_fail_by_type["error"] = _gov_tx_fail_by_type.get("error", 0) + 1
        raise


@guardian("tx_sweep")
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
                await tx_rollback(tx_id=tx.id, source="ttl")
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
        if rec.content_ref:
            from app.db.repos import set_tier_by_content
            await set_tier_by_content(session, content_sha=rec.content_ref, tier=_cold)
        else:
            await update_artifact_tier(session, artifact_id=rec.id, tier=_cold)
    return {"ok": True, "tier": _cold}


async def pin_tier_artifact(*, task_id: str, rel_path: str, pinned: bool,
                            owner_id: str | None = None) -> dict:
    """N1 置顶热：设/解 tier_pinned（冷化/backfill 排除置顶）。

    ``owner_id`` 仅供限额核算（普通用户传 user.id；admin 传 None=不限）。
    越权（文件不属于访问者任务）由 API 层 ``_require_can_edit`` 统一 404。
    """
    s = get_settings()
    if not (_enabled() and s.TIER_ENABLED):
        return {"ok": False, "reason": "disabled"}
    from app.db import repos as repos_mod

    factory = get_session_factory()
    async with factory() as session:
        rec = await repos_mod.get_artifact_by_rel(session, task_id=task_id,
                                                  rel_path=rel_path)
        if rec is None:
            return {"ok": False, "reason": "not_found"}
        if pinned:
            cnt, byt = await repos_mod.pinned_stats(session, owner_id=owner_id or "")
            if cnt >= s.TIER_PINNED_MAX_COUNT:
                return {"ok": False, "reason": "pinned_limit_count",
                        "limit": s.TIER_PINNED_MAX_COUNT}
            if s.QUOTA_ENABLED and s.QUOTA_TOTAL_MAX_BYTES > 0:
                cap = int(s.QUOTA_TOTAL_MAX_BYTES * s.TIER_PINNED_MAX_RATIO)
                if byt + rec.size > cap:
                    return {"ok": False, "reason": "pinned_limit_bytes", "cap": cap}
            await repos_mod.set_tier_pinned(session, artifact_id=rec.id, pinned=True)
        else:
            await repos_mod.set_tier_pinned(session, artifact_id=rec.id, pinned=False)
    await _audit_gov(task_id=task_id, owner_id=rec.owner_id or "",
                     action="governance.tier.pin",
                     detail={"rel_path": rel_path, "pinned": pinned})
    return {"ok": True, "pinned": pinned}


@guardian("cold_sweep")
async def cold_sweep_once(session_factory, backend) -> int:
    """守护分层（N1 状态机 hot↔warm→cold）：按 ``last_access`` 仅向下衰减、访问回流。

    - hot → warm（超 WARM_AGE，仅元数据标记，物理不动）；
    - warm → cold（超 COLD_ACCESS_AGE，物理归档 + 同步 content 引用）。
    - 置顶文件（tier_pinned）永不参与降冷。冷却期内文件不降冷（防抖）。
    """
    if not _enabled():
        return 0
    s = get_settings()
    if not s.TIER_ENABLED:
        return 0
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from app.db.models import Artifact
    from app.db.repos import set_tier_by_content, update_artifact_tier

    now = datetime.now(UTC)
    cool = max(60, s.TIER_COOL_DOWN)
    warm_cutoff = now - timedelta(seconds=max(60, s.TIER_WARM_AGE, cool))
    cold_cutoff = now - timedelta(seconds=max(60, s.TIER_COLD_ACCESS_AGE, cool))
    archived = 0
    seen_content: set[str] = set()
    try:
        async with session_factory() as session:
            base = (Artifact.status == _available) & (Artifact.tier_pinned.is_(False))
            age = func.coalesce(Artifact.last_access, Artifact.created_at)
            # 阶段1 hot → warm
            warm_rows = (await session.execute(
                select(Artifact).where(base, Artifact.tier == _hot,
                                       age < warm_cutoff)
            )).scalars().all()
            for rec in warm_rows:
                try:
                    await update_artifact_tier(session, artifact_id=rec.id, tier=_warm)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("温降失败 %s: %s", rec.key, exc)
            # 阶段2 warm → cold
            cold_rows = (await session.execute(
                select(Artifact).where(base, Artifact.tier == _warm,
                                       age < cold_cutoff)
            )).scalars().all()
            for rec in cold_rows:
                if rec.content_ref and rec.content_ref in seen_content:
                    continue
                try:
                    ok = await backend.archive_cold(rec.key)
                    if ok:
                        if rec.content_ref:
                            content_sha = rec.content_ref
                            await set_tier_by_content(session, content_sha=content_sha,
                                                      tier=_cold)
                            seen_content.add(content_sha)
                        else:
                            await update_artifact_tier(session, artifact_id=rec.id,
                                                       tier=_cold)
                        archived += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("冷化失败 %s: %s", rec.key, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("分层扫描失败：%s", exc)
    return archived


async def touch_artifact(*, task_id: str, rel_path: str) -> None:
    """热度埋点（P6-2 O1）：完整文件读取时刷新 last_access。

    - 总闸/分层关闭 → no-op；
    - 距上次更新 < TIER_TOUCH_TTL → 跳过（防写入放大）；
    - 冷却期内 → 仅 access_count+=1，不刷新 last_access（防刷活）；
    - 仅完整读（非 Range/分页多读）调用，直链/版本读由调用方决定不调用。
    """
    if not (_enabled() and get_settings().TIER_ENABLED):
        return
    from datetime import UTC, datetime

    from app.db.repos import get_artifact_by_rel, update_artifact_access

    now = datetime.now(UTC)
    s = get_settings()
    async with get_session_factory()() as session:
        rec = await get_artifact_by_rel(session, task_id=task_id, rel_path=rel_path)
        if rec is None:
            return
        now_naive = now.replace(tzinfo=None)
        last = rec.last_access
        if last is not None and last.tzinfo is not None:
            last = last.replace(tzinfo=None)  # 兼容 aware 来源（PG）
        if last is not None and (now_naive - last).total_seconds() < max(60, s.TIER_TOUCH_TTL):
            return  # 节流：短时重复完整读不写库
        in_cool_down = last is not None and (now_naive - last).total_seconds() < max(
            60, s.TIER_COOL_DOWN)
        await update_artifact_access(
            session, artifact_id=rec.id, last_access=now_naive,
            full=True, in_cool_down=in_cool_down)
        # N1 访问回流：hot/warm/cold 被读到即回热（时间向上重置）
        if rec.tier != _hot:
            from app.db.repos import set_tier_by_content, update_artifact_tier
            if rec.content_ref:
                await set_tier_by_content(session, content_sha=rec.content_ref, tier=_hot)
            else:
                await update_artifact_tier(session, artifact_id=rec.id, tier=_hot)


# ===========================================================================
# P6-2 O2 · 配额智能（历史采样 + 趋势预测 + 报表 + 成本核算）
# ===========================================================================

_QUOTA_SAMPLE_LOCK_KEY = "artifacts:quota:sample_lock"


@guardian("quota_history_sweep")
async def quota_history_sweep_once(session_factory, redis=None) -> dict:
    """配额历史采样（守护，P6-2 O2）：遍历有额度的 owner 各写入一条当前用量。

    - 总闸/采样开关关闭 → no-op；
    - Redis SET NX EX 全局单实例锁，多实例仅一个采样；
    - 单 owner 写入失败重试 2 次，仍失败记告警跳过（不漏采由下一轮补）。
    """
    s = get_settings()
    if not (_enabled() and s.QUOTA_HISTORY_ENABLED):
        return {"enabled": False}
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.db import repos as repos_mod
    from app.db.models import QuotaUsage

    if redis is not None:
        try:
            got = await redis.set(_QUOTA_SAMPLE_LOCK_KEY, s.worker_id, nx=True,
                                  ex=max(60, s.QUOTA_HISTORY_INTERVAL))
        except Exception:  # noqa: BLE001
            got = True  # 锁不可用退化为无锁（仅告警）
            logger.warning("配额采样锁获取失败，退化为无锁执行")
        if not got:
            return {"locked_out": 1, "sampled": 0}

    sampled = 0
    failed = 0
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with session_factory() as session:
            owners = (await session.execute(select(QuotaUsage.owner_id))).scalars().all()
        for owner in owners:
            if not owner:
                continue
            # 独立提交：读取用量 + 写采样（单 owner 失败不影响整体），失败重试 2 次
            ok = False
            for _ in range(3):
                try:
                    async with session_factory() as session:
                        used = await repos_mod.get_quota_used(session, owner_id=owner)
                    async with session_factory() as session:
                        await repos_mod.add_quota_history(
                            session, owner_id=owner, used_bytes=used, recorded_at=now)
                    ok = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if ok:
                sampled += 1
            else:
                logger.warning("配额采样失败 owner=%s", owner)
                failed += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("配额采样扫描失败：%s", exc)
    return {"sampled": sampled, "failed": failed}


@guardian("quota_history_prune")
async def quota_history_prune_once(session_factory) -> int:
    """清理超保留窗口的配额历史（P6-2 O2 O2-3）。返回删除行数。"""
    s = get_settings()
    if not (_enabled() and s.QUOTA_HISTORY_ENABLED):
        return 0
    from datetime import UTC, datetime, timedelta

    from app.db import repos as repos_mod

    cutoff = datetime.now(UTC) - timedelta(days=max(1, s.QUOTA_HISTORY_RETENTION_DAYS))
    async with session_factory() as session:
        return await repos_mod.prune_quota_history(session, older_than=cutoff)


def _quota_trend(history: list, total: int) -> dict | None:
    """线性最小二乘预测：斜率>0 且 r>=阈值才给 ETA；否则 None。"""
    s = get_settings()
    n = len(history)
    if n < 2 or not total or total <= 0:
        return None
    xs = [t.timestamp() for t, _ in history]
    ys = [float(u) for _, u in history]
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    varx = sum((x - mx) ** 2 for x in xs)
    if varx == 0:
        return None
    slope = cov / varx  # 字节/秒
    if slope <= 0:  # 斜率≤0 → 用量稳定/下降，不做耗尽预测
        return {"slope_bytes_per_sec": 0.0, "eta_hours": None, "trend": "stable"}
    # 相关系数
    vary = sum((y - my) ** 2 for y in ys) or 1.0
    r = cov / ((varx * vary) ** 0.5) if (varx * vary) > 0 else 0.0
    if r < s.QUOTA_HISTORY_PREDICT_R2:  # 低相关 → 不可预测
        return {"slope_bytes_per_sec": slope,
                "eta_hours": None, "trend": "unpredictable", "r": round(r, 3)}
    remaining = total - ys[-1]
    eta_hours = None
    if remaining > 0 and slope > 0:
        eta_hours = (remaining / slope) / 3600.0
    alert = None
    alert_hours = s.QUOTA_ETA_ALERT_THRESHOLD_HOURS
    if eta_hours is not None:
        alert = "high" if eta_hours <= alert_hours else \
            "low" if eta_hours <= 24 * 30 else None
    return {"slope_bytes_per_sec": slope, "eta_hours": eta_hours,
            "trend": "growing", "r": round(r, 3), "alert": alert}


async def quota_report_for(owner_id: str | None) -> dict:
    """配额智能报表（P6-2 O2）：趋势 + 峰值 + 清理建议 + 成本核算。"""
    s = get_settings()
    if not (_enabled() and s.QUOTA_HISTORY_ENABLED):
        return {}
    from app.db import repos as repos_mod

    factory = get_session_factory()
    total = s.QUOTA_TOTAL_MAX_BYTES if s.QUOTA_ENABLED else 0
    async with factory() as session:
        used = await repos_mod.get_quota_used(session, owner_id=owner_id or "")
        history = await repos_mod.get_quota_history(
            session, owner_id=owner_id or "", limit=s.QUOTA_HISTORY_POINTS)
        stats = await repos_mod.artifact_stats(session, owner_id=owner_id or "")
    trend = _quota_trend(history, total)
    peak = {"used_bytes": used, "percent": round((used / total) * 100, 1) if total else 0.0}
    # 清理建议：回收站(deleted 仍占配额) + 冷文件，按 size 降序
    suggestions = []
    async with factory() as session:
        rows = await _recycle_tier_suggestions(session, owner_id=owner_id)
        for row in rows:
            suggestions.append({
                "rel_path": row.rel_path, "size": row.size, "tier": row.tier,
                "status": row.status, "task_id": row.task_id or "",
            })
    # 成本核算
    hot = stats.get("hot_bytes", 0)
    cold = stats.get("cold_bytes", 0)
    period = max(1, s.QUOTA_COST_PERIOD_DAYS)
    cost = {
        "period_days": period,
        "hot": round((hot / (1024 ** 3)) * (s.QUOTA_COST_HOT_PER_GB or 0) * period, 4),
        "cold": round((cold / (1024 ** 3)) * (s.QUOTA_COST_COLD_PER_GB or 0) * period, 4),
    }
    # N1 冷化释放预估（warm 可冷化候选；置顶剔除；去重按物理）
    cold_eligible = {"logical_bytes": 0, "physical_bytes": 0}
    async with factory() as session:
        lg, ph = await repos_mod.warm_eligible_bytes(
            session, dedup=_dedup_enabled())
        cold_eligible = {"logical_bytes": lg, "physical_bytes": ph}
    # N4 配额额度建议（保守下限 + 冷热拆分 + 可解释）
    suggested_quota = _suggest_quota(used=used, total=total, trend=trend,
                                     peak=peak.get("used_bytes", 0),
                                     hot=stats.get("hot_bytes", 0),
                                     cold=stats.get("cold_bytes", 0),
                                     history_len=len(history), s=s)
    return {
        "owner_id": owner_id or "",
        "quota_total": total, "quota_used": used,
        "trend": trend, "peak": peak,
        "suggestions": sorted(suggestions, key=lambda x: x["size"], reverse=True)[:20],
        "cost": cost,
        "cold_eligible": cold_eligible,  # 去重场景实际释放可能小于 logical_bytes
        "suggested_quota": suggested_quota,
    }


def _suggest_quota(*, used, total, trend, peak, hot, cold, history_len, s) -> dict:
    """N4 配额建议：下限=当前×1.1；低置信度/无历史走峰值×1.2 或默认 5GB；下降趋势维持不降级。"""
    base = max(int(used * 1.1), used)
    reason = []
    confidence = "low"
    if history_len < 2:
        quota = max(base, 5 * 1024 ** 3)
        reason.append("无足够历史，采用初始默认值(5GB)或当前用量基线")
        confidence = "low"
    elif trend and trend.get("trend") == "growing" and trend.get("eta_hours"):
        slope = trend.get("slope_bytes_per_sec", 0.0)
        proj = int(used + slope * 3600 * 24 * 30)  # 未来30天
        quota = max(base, proj)
        reason.append(f"按近30天趋势外推需求 ≈ {proj} bytes")
        confidence = "medium" if (trend.get("r") or 0) >= 0.9 else "low"
    else:
        quota = max(base, int(peak * 1.2))
        reason.append("用量稳定/下降或低置信度，按峰值×1.2 保守建议（不主动降级）")
        confidence = "medium"
    # 冷热拆分建议 + 成本优化提示
    hot_sug = max(int(hot * 1.1), hot)
    cold_sug = max(int(cold * 1.1), cold)
    return {
        "quota": quota, "confidence": confidence, "reason": reason,
        "components": {"hot": hot_sug, "cold": cold_sug},
        "cost_advice": {
            "hint": "可将低频数据冷归档以降本",
            "cold_bytes": cold, "hot_bytes": hot,
        },
    }


async def _recycle_tier_suggestions(session, *, owner_id):
    """返回 owner 的 deleted(回收站) 与 cold 产物记录（清理建议候选）。"""
    from sqlalchemy import or_, select

    from app.db.models import Artifact

    rows = (await session.execute(
        select(Artifact).where(
            Artifact.owner_id == owner_id,
            or_(Artifact.status == _deleted, Artifact.tier == _cold),
        ).order_by(Artifact.size.desc()).limit(100)
    )).scalars().all()
    return rows


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
    """统一物理删除编排（🔴5 删除顺序唯一入口）：逻辑判断 → 删物理 → 删元表 → 冲正配额。

    - 非去重（content_ref 空）→ 删物理 key；
    - 去重（content_ref 有值）→ ``content_release``，refs 归 0 才删物理 + 删 content 记录；
      共享（refs>0）→ 不删物理，仅删本 Artifact 行（按 id，防误删同 key 共享行）。
    每步失败重试一次；失败保留元表交由对账兜底并审计。
    """
    backend = backend or get_backend()
    key = rec.key
    content_sha = rec.content_ref
    factory = get_session_factory()
    # 1) 逻辑判断 + 删物理（去重按 refs 决定；非去重直接删）
    if content_sha:
        async with factory() as session:
            refs = await repos.content_release(session, sha256=content_sha)
        if refs <= 0:
            try:
                deleted = await backend.delete(key)
                if not deleted:
                    deleted = await backend.delete(key)
                if deleted:
                    async with factory() as session:
                        await repos.content_delete(session, sha256=content_sha)
            except StorageError as exc:
                await _audit_gov(task_id=rec.task_id or "", owner_id=rec.owner_id or "",
                                 action="governance.delete.storage_fail",
                                 detail={"key": key, "reason": reason}, ok=False,
                                 error=str(exc))
                logger.error("去重物理删除存储失败 key=%s sha=%s: %s", key, content_sha[:8], exc)
                return False
            except RepositoryError as exc:
                await _audit_gov(task_id=rec.task_id or "", owner_id=rec.owner_id or "",
                                 action="governance.delete.content_fail",
                                 detail={"key": key, "reason": reason}, ok=False,
                                 error=str(exc))
                return False
    else:
        try:
            deleted = await backend.delete(key)
            if not deleted:
                deleted = await backend.delete(key)
        except StorageError as exc:
            await _audit_gov(task_id=rec.task_id or "", owner_id=rec.owner_id or "",
                             action="governance.delete.storage_fail",
                             detail={"key": key, "reason": reason}, ok=False,
                             error=str(exc))
            logger.error("物理删除存储失败 key=%s: %s", key, exc)
            return False
    # 2) 删元表（按 id，防去重共享 key 误删其他行）
    async with factory() as session:
        await repos.delete_artifact(session, artifact_id=rec.id)
        # 3) 冲正配额（effective，floor 0）
        if rec.owner_id and get_settings().QUOTA_ENABLED \
                and not (get_settings().QUOTA_EXEMPT_SYSTEM and rec.owner_id == "system"):
            delta = _effective_size(rec.size, tier=rec.tier)
            await repos.bump_quota(session, owner_id=rec.owner_id, delta=-delta)
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


@guardian("reconcile")
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
    # O4-E：去重物理按 content.refs>0 为引用判据（共享必不判 orphan，防误删）
    from app.storage.base import is_dedup_key

    refs_by_sha: dict[str, int] = {}
    async with session_factory() as session:
        for c in await repos.content_all_refs(session):
            refs_by_sha[c.sha256] = c.refs
    # 3) orphan：存储有、DB 无 → 若为去重物理且 refs>0 则保留（共享）；否则清理
    for k in store_keys:
        if k not in db_keys:
            if is_dedup_key(k) and refs_by_sha.get(k.rsplit("/", 1)[-1], 0) > 0:
                continue  # 去重物理仍被引用，绝不判 orphan（🔴O4-E-1）
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


@guardian("meta_init")
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
                    session, task_id=task_id, rel_path=rel_path, key=key,
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


@guardian("recycle_sweep")
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
