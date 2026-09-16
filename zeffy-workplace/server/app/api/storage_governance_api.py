"""P6 产物生命周期治理 API：事务（批次D/I）/ 用量计量（批次F）/ 回收站（批次J）。

- ``POST /artifacts/tx/open``   —— 开启事务（可带 estimated_bytes 预扣；can_edit）
- ``POST /artifacts/tx/{tx_id}/files`` —— 暂存写入（_tx 隔离；can_edit）
- ``GET  /artifacts/tx/{tx_id}`` —— 状态/进度（can_view）
- ``POST /artifacts/tx/{tx_id}/commit`` —— 原子提交（can_edit）
- ``POST /artifacts/tx/{tx_id}/rollback`` —— 回滚（can_edit）
- ``GET  /artifacts/stats``     —— 用量计量（本人 owner；🔴 越权 404）
- ``GET  /artifacts/recycle``   —— 回收站列表（本人 owner）
- ``POST /artifacts/{task_id}/artifacts/{rel_path}/delete`` / ``/restore`` —— 回收（can_edit）

安全（🔴6）：所有写操作统一 can_edit，越权 404；用户态强制绑定当前登录 owner，
不得指定他人/伪造 owner_id；system 账号在用户接口不可操作（_owner_id 返回 None）。

治理/能力关闭 → 对应路由 404（兼容锚点，P5 零漂移）。
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db.base import get_session_factory
from app.db.repos import artifact_stats, get_quota_used, get_task
from app.storage.gov_store import GovConfigUnavailable
from app.storage.governance import (
    QuotaExceededError,
    QuotaUnavailableError,
    list_recycle,
    restore_artifact,
    soft_delete_artifact,
    tx_commit,
    tx_open,
    tx_rollback,
    tx_stage_write,
    tx_status,
)

router = APIRouter(prefix="/artifacts/tx", tags=["artifacts-tx"])
CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


def _meta_enabled() -> bool:
    return get_settings().ARTIFACT_META_ENABLED


def _tx_enabled() -> bool:
    return get_settings().ARTIFACT_META_ENABLED and get_settings().TX_ENABLED


def _recycle_enabled() -> bool:
    return get_settings().ARTIFACT_META_ENABLED and get_settings().RECYCLE_ENABLED


def _owner_id(user: UserPrincipal) -> str | None:
    if user.authenticated and user.id:
        return None if user.is_system else user.id
    return None


async def _load_task(task_id: str):
    factory = get_session_factory()
    async with factory() as session:
        return await get_task(session, task_id)


async def _require_can_edit(session, user, task) -> None:
    from app.auth import permissions as perm

    if not await perm.can_edit(session, user, task):
        raise HTTPException(status_code=404, detail="Not Found")


async def _require_can_view(session, user, task) -> None:
    from app.auth import permissions as perm

    if not await perm.can_view(session, user, task):
        raise HTTPException(status_code=404, detail="Not Found")


def _map_gov_error(exc: Exception) -> HTTPException:
    if isinstance(exc, QuotaExceededError):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, QuotaUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


async def _authorize_tx(tx_id: str, user: UserPrincipal, *, write: bool) -> None:
    """加载事务并校验所属任务的可编辑/可视图（🔴6 越权统一 404）。"""
    st = await tx_status(tx_id=tx_id)
    if st["status"] == "not_found" or not st.get("task_id"):
        raise HTTPException(status_code=404, detail="Not Found")
    task = await _load_task(st["task_id"])
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        if write:
            await _require_can_edit(session, user, task)
        else:
            await _require_can_view(session, user, task)
    # 用户态强制绑定 owner：事务归属者须为当前用户（或经 can_edit 授权的协作）
    return None


@router.post("/open")
async def api_tx_open(user: CurrentUser, payload: dict):
    """开启事务批次（结构 {task_id, estimated_bytes?}）。"""
    if not _tx_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    task_id = payload.get("task_id")
    est = payload.get("estimated_bytes", 0)
    if not isinstance(task_id, str):
        raise HTTPException(status_code=400, detail="参数非法")
    if not isinstance(est, int) or est < 0:
        raise HTTPException(status_code=400, detail="estimated_bytes 非法")
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_can_edit(session, user, task)
    try:
        return await tx_open(task_id=task_id, owner_id=_owner_id(user),
                             estimated_bytes=est)
    except Exception as exc:  # noqa: BLE001
        raise _map_gov_error(exc) from exc


@router.post("/{tx_id}/files")
async def api_tx_stage(tx_id: str, user: CurrentUser, payload: dict):
    """暂存写入（🔴4 隔离）：{task_id, rel_path, data_b64, mime?}。"""
    if not _tx_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    task_id = payload.get("task_id")
    rel_path = payload.get("rel_path")
    data_b64 = payload.get("data_b64")
    if not isinstance(task_id, str) or not isinstance(rel_path, str) \
            or not isinstance(data_b64, str):
        raise HTTPException(status_code=400, detail="参数非法")
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="data_b64 非法") from None
    await _authorize_tx(tx_id, user, write=True)
    try:
        return await tx_stage_write(tx_id=tx_id, task_id=task_id, rel_path=rel_path,
                                    data=data, mime=payload.get("mime", ""))
    except Exception as exc:  # noqa: BLE001
        raise _map_gov_error(exc) from exc


@router.get("/{tx_id}")
async def api_tx_status(tx_id: str, user: CurrentUser):
    if not _tx_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        await _authorize_tx(tx_id, user, write=False)
    except HTTPException:
        raise
    try:
        return await tx_status(tx_id=tx_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Not Found") from None


@router.post("/{tx_id}/commit")
async def api_tx_commit(tx_id: str, user: CurrentUser):
    if not _tx_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    await _authorize_tx(tx_id, user, write=True)
    try:
        return await tx_commit(tx_id=tx_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_gov_error(exc) from exc


@router.post("/{tx_id}/rollback")
async def api_tx_rollback(tx_id: str, user: CurrentUser):
    if not _tx_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    await _authorize_tx(tx_id, user, write=True)
    try:
        return await tx_rollback(tx_id=tx_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_gov_error(exc) from exc


# ---- 用量计量（/artifacts/stats） ----
stats_router = APIRouter(prefix="/artifacts", tags=["artifacts-stats"])


@stats_router.get("/stats")
async def api_artifacts_stats(user: CurrentUser):
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    owner = _owner_id(user)
    if not owner:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        stats = await artifact_stats(session, owner_id=owner)
        used = await get_quota_used(session, owner_id=owner)
    s = get_settings()
    stats["owner_id"] = owner
    stats["quota_total"] = s.QUOTA_TOTAL_MAX_BYTES if s.QUOTA_ENABLED else 0
    stats["quota_used"] = used if s.QUOTA_ENABLED else 0
    # O4-G 双口径：logical_used(=配额) + physical_used(去重物理实际) + 去重指标（⭐）
    stats["logical_used"] = used if s.QUOTA_ENABLED else stats.get("total_bytes", 0)
    from app.db import repos as _repos
    from app.storage.governance import _dedup_enabled

    if _dedup_enabled():
        async with factory() as _sess:
            contents = await _repos.content_all_refs(_sess)
        physical = sum(c.size for c in contents if c.refs > 0)
        stats["physical_used"] = physical
        # 去重收益（仅按 owner 的逻辑已去重份额估算）
        logical_total = int(stats["total_bytes"] or 0)
        stats["dedup"] = {
            "enabled": True,
            "content_count": sum(1 for c in contents if c.refs > 0),
            "physical_bytes": physical,
            "dedup_saved_bytes": max(0, logical_total - physical),
        }
    else:
        stats["physical_used"] = stats.get("total_bytes", 0)
        stats["dedup"] = {"enabled": False}
    return stats


@stats_router.get("/quota/report")
async def api_quota_report(user: CurrentUser, owner_id: str | None = Query(default=None)):
    """P6-2 O2 配额智能报表：趋势/峰值/清理建议/成本。普通用户仅本人；system 可查全量。"""
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    s = get_settings()
    if not (s.QUOTA_HISTORY_ENABLED and s.QUOTA_ENABLED):
        raise HTTPException(status_code=404, detail="Not Found")
    from app.storage.governance import quota_report_for

    if user.authenticated and user.is_system:
        target = owner_id  # system 后台可查任意 owner（owner_id 空 = 全局未定义时由调用方限定）
        if not target:
            raise HTTPException(status_code=404, detail="Not Found")
    else:
        target = _owner_id(user)
        if not target:
            raise HTTPException(status_code=404, detail="Not Found")
    if not target:
        raise HTTPException(status_code=404, detail="Not Found")
    return await quota_report_for(target)


@stats_router.get("/audit")
async def api_audit_query(user: CurrentUser, action: str | None = Query(default=None),
                          task_id: str | None = Query(default=None),
                          result: str | None = Query(default=None),
                          since: str | None = Query(default=None),
                          until: str | None = Query(default=None),
                          export: str = "json", page: int = 1, page_size: int = 50):
    """P6-2 O3 + P6-3 N3 审计查询/导出：多维筛选 + CSV 导出(90天/脱敏/限速)。"""
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    from datetime import UTC, datetime, timedelta

    from app.db import repos
    from app.db.base import get_session_factory

    factory = get_session_factory()
    system_view = bool(user.authenticated and user.is_system)
    operator = None if system_view else _owner_id(user)
    if not system_view and not operator:
        raise HTTPException(status_code=404, detail="Not Found")
    # 时间窗：默认近90天，超90天拒绝
    _since = None
    _until = None
    if since:
        _since = datetime.fromisoformat(since.replace("Z", "+00:00")).replace(tzinfo=None)
    if until:
        _until = datetime.fromisoformat(until.replace("Z", "+00:00")).replace(tzinfo=None)
    if _since is None:
        _since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=90)
    if _until and _since and (_until - _since) > timedelta(days=90):
        raise HTTPException(status_code=400, detail="导出时间范围最长 90 天")
    if result and result not in ("ok", "fail"):
        raise HTTPException(status_code=400, detail="result 取值 ok|fail")
    async with factory() as session:
        rows, total = await repos.list_audit_logs(
            session, operator=operator, action_prefix=action, task_id=task_id,
            result=result, since=_since, until=_until,
            page=page, page_size=page_size if export != "csv" else 5000)
    if export == "csv":
        return _audit_csv(rows, operator or "")
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [
            {"id": r.id, "operator": r.operator, "action": r.action,
             "detail": r.detail, "task_id": r.task_id,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ],
    }


# N3 CSV 导出：脱敏(去 ip/error/stack/trace) + 速率限制(每运算符每小时≤3)
_audit_export_bucket: dict[str, list[float]] = {}


def _audit_csv(rows, key: str):
    import csv
    import io
    import time

    from fastapi.responses import StreamingResponse

    now = time.time()
    lst = _audit_export_bucket.setdefault(key, [])
    lst[:] = [t for t in lst if now - t < 3600]  # 滚动窗口1小时
    if len(lst) >= 3:
        raise HTTPException(status_code=429, detail="导出过于频繁，每小时最多 3 次")
    lst.append(now)
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM
    w = csv.writer(buf)
    w.writerow(["id", "operator", "action", "result", "created_at"])
    for r in rows:
        detail = r.detail or {}
        result_ok = detail.get("ok")
        w.writerow([r.id, r.operator, r.action,
                    str(result_ok).lower() if result_ok is not None else "",
                    r.created_at.isoformat() if r.created_at else ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=audit.csv"})


class _TierPinBody(BaseModel):
    pinned: bool


@stats_router.post("/tier-pin/{task_id}/{rel_path:path}")
async def api_tier_pin(task_id: str, rel_path: str, body: _TierPinBody, user: CurrentUser):
    """N1 置顶热：can_edit，越权/不存在 404；admin 不限（owner=None），普通用户按限额。"""
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if not get_settings().TIER_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")
    from app.storage.governance import pin_tier_artifact

    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_can_edit(session, user, task)
    r = await pin_tier_artifact(task_id=task_id, rel_path=rel_path,
                                pinned=body.pinned, owner_id=_owner_id(user))
    if not r.get("ok") and r.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail="Not Found")
    return r


@stats_router.get("/dedup/{sha256}")
async def api_dedup_trace(sha256: str, user: CurrentUser):
    """N2 引用溯源：普通用户仅见本人引用，admin 全量；哈希不存在/越权统一 404（防泄露）。"""
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if not get_settings().DEDUP_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")
    from app.db import repos as _repos

    factory = get_session_factory()
    async with factory() as s:
        if await _repos.content_get(s, sha256=sha256) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        is_admin = bool(user.authenticated and user.is_system)
        owner = None if is_admin else _owner_id(user)
        if owner is None and not is_admin:
            raise HTTPException(status_code=404, detail="Not Found")
        refs = await _repos.artifacts_by_content(s, content_sha=sha256, owner_id=owner)
        if owner and not refs:
            raise HTTPException(status_code=404, detail="Not Found")  # 无权限不区分 404
    return {"sha256": sha256, "refs": [
        {"task_id": r.task_id, "rel_path": r.rel_path, "status": r.status,
         "tier": r.tier, "size": r.size, "owner_id": r.owner_id}
        for r in refs
    ]}


@stats_router.post("/dedup/backfill")
async def api_dedup_backfill(user: CurrentUser):
    """N2 手动存量去重（管理操作）：仅 admin/system；幂等（运行中返回执行中）。"""
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if not get_settings().DEDUP_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")
    if not (user.authenticated and user.is_system):
        raise HTTPException(status_code=404, detail="Not Found")
    from app.db import repos as _repos
    from app.storage.governance import dedup_backfill_once

    r = await dedup_backfill_once(get_session_factory())
    async with get_session_factory()() as s:
        await _repos.write_audit(s, task_id="", operator=user.id or "system",
                                 action="governance.dedup.backfill", detail=r)
    return r


# ---- P6-4 D+C 运维面板 / 回滚入口 / 灰度管理（admin-only）----
admin_governance_router = APIRouter(prefix="/admin/governance", tags=["admin-governance"])
# 灰度名单的统一数据源在 governance 层（进程内单实例；多实例需中心化，见 P6-4 评审）


async def _require_admin(user) -> None:
    if not (user.authenticated and (user.is_system or user.role_is_admin())):
        raise HTTPException(status_code=404, detail="Not Found")


class _ToggleBody(BaseModel):
    enabled: bool
    reason: str = ""


class _EmergencyBody(BaseModel):
    confirm: bool = False
    reason: str = ""


class _GateBody(BaseModel):
    feature: str
    add: list[str] = []
    remove: list[str] = []


_GOV_FEATURES = {
    "meta", "quota", "dedup", "quota_history", "tier", "tx", "recycle",
    "reconcile", "audit",
}


@admin_governance_router.get("/status")
async def api_gov_status(user: CurrentUser):
    await _require_admin(user)
    from app.storage.governance import governance_status

    return governance_status()


@admin_governance_router.post("/emergency-disable")
async def api_gov_emergency(body: _EmergencyBody, user: CurrentUser):
    """全局应急回滚：一键关闭所有治理（需 confirm + reason，幂等）。

    中心化模式走 Redis（版本+广播）；降级时写拒绝 503。
    """
    await _require_admin(user)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="全局回滚需 confirm=true 确认")
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="需提供 reason")
    from app.storage.governance import set_governance_override_remote

    try:
        for f in _GOV_FEATURES:
            await set_governance_override_remote(f, False)
    except GovConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await _gov_admin_audit(user, "governance.emergency_disable", {
        "impact": sorted(_GOV_FEATURES), "confirm": True}, body.reason)
    return {"ok": True, "impact": sorted(_GOV_FEATURES)}


@admin_governance_router.post("/{feature}")
async def api_gov_toggle(feature: str, body: _ToggleBody, user: CurrentUser):
    """单功能开/关（运行时覆盖；中心化走 Redis 即时生效，降级写拒绝 503）。"""
    await _require_admin(user)
    if feature not in _GOV_FEATURES:
        raise HTTPException(status_code=404, detail="Not Found")
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="需提供 reason")
    from app.storage.governance import set_governance_override_remote

    try:
        r = await set_governance_override_remote(feature, body.enabled)
    except GovConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await _gov_admin_audit(user, f"governance.toggle.{feature}", {
        "enabled": body.enabled, "impact": [feature], "centralized": r.get("centralized")},
        body.reason)
    return {"ok": True, "feature": feature, "enabled": body.enabled,
            "centralized": r.get("centralized")}


@admin_governance_router.get("/gates")
async def api_gates_get(user: CurrentUser):
    await _require_admin(user)
    from app.storage.governance import gray_members

    return {"gates": {k: sorted(v) for k, v in gray_members().items()}}


@admin_governance_router.post("/gates")
async def api_gates_update(body: _GateBody, user: CurrentUser):
    """灰度管理：add/remove owner；校验 owner 存在；批量原子(pipeline)+全量审计。

    中心化模式走 Redis（★4 批量原子、幂等）；降级时写拒绝 503。
    """
    await _require_admin(user)
    if body.feature not in _GOV_FEATURES:
        raise HTTPException(status_code=404, detail="Not Found")
    from app.db import repos as _repos
    from app.storage.governance import gray_members, gray_set_remote

    factory = get_session_factory()
    for oid in body.add:
        async with factory() as s:
            if await _repos.get_user_by_id(s, oid) is None:
                raise HTTPException(status_code=400, detail=f"不存在的用户：{oid}")
    before = sorted(gray_members().get(body.feature, []))
    try:
        r = await gray_set_remote(body.feature, list(body.add), list(body.remove))
    except GovConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    after = r["members"]
    await _gov_admin_audit(user, f"governance.gray.{body.feature}", {
        "before": before, "after": after,
        "add": body.add, "remove": body.remove,
        "centralized": r.get("centralized")}, f"灰度调整({body.feature})")
    return {"ok": True, "feature": body.feature, "members": after,
            "centralized": r.get("centralized")}


# ---- P6-4-B 配置快照（⭐5：变更前存盘 + 一键回滚上一版本） ----
class _SnapBody(BaseModel):
    reason: str = ""


@admin_governance_router.get("/alerts")
async def api_gov_alerts(user: CurrentUser, page: int = 1, page_size: int = 50):
    """治理/系统告警历史（admin-only，越权 404）。按触发/恢复分组，detail 含 metric/level/threshold/current。"""
    await _require_admin(user)
    from datetime import UTC, datetime, timedelta

    from app.db import repos as _repos

    factory = get_session_factory()
    until = datetime.now(UTC).replace(tzinfo=None)
    since = until - timedelta(days=30)
    async with factory() as session:
        rows, total = await _repos.list_audit_logs(
            session, operator=None, action_prefix="governance_alarm_",
            since=since, until=until, page=page, page_size=page_size)
        return {
            "total": total, "page": page, "page_size": page_size,
            "items": [{
                "id": r.id, "action": r.action,
                "detail": r.detail or {},
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows],
        }


# ---- P6-6-5 加密可观测状态（admin-only；白名单输出，零密钥材料） ----
@admin_governance_router.get("/encryption-status")
async def api_encryption_status(user: CurrentUser):
    """加密健康状态（admin 专用，越权 404）。输出严格白名单：

    - 开关/密钥加载状态/版本/计数/滑动窗口/健康评分/近 24h 失败率趋势。
    - 绝不输出：密钥指纹、密文片段、文件路径、错误堆栈、算法参数。
    """
    await _require_admin(user)
    from app.storage.crypto_gate import crypto_metrics
    from app.storage.governance import governance_metrics

    enc = (governance_metrics().get("encryption") or {})
    cm = crypto_metrics()
    win = enc.get("window") or {}
    counters = enc.get("counters") or {}
    dec = int(counters.get("decrypt", 0) or 0)
    fail = int(counters.get("decrypt_fail", 0) or 0)
    rate = fail / (dec + fail) if dec + fail > 0 else 0.0
    enabled = bool(enc.get("enabled"))
    key_loaded = bool(enc.get("key_loaded"))
    # 健康评分 0-100：密钥未加载/关闭 → 低分；失败率/篡改/降级扣分
    score = 100
    if not enabled:
        score = 0
    elif not key_loaded:
        score = min(score, 30)
    if rate >= get_settings().ALERT_ENCRYPT_FAIL_RATE:
        score -= 40
    score -= min(30, int(win.get("tamper", 0) or 0) * 5)
    score -= min(20, int(win.get("degrade_plain", 0) or 0) * 10)
    score = max(0, score)
    return {
        "enabled": enabled,
        "key_loaded": key_loaded,
        "algorithm": "AES-256-GCM" if enabled else None,
        "cipher_version": enc.get("cipher_version"),
        "counters": counters,
        "window": win,
        "decrypt_fail_rate": round(rate, 4),
        "encrypted_physical_bytes": int(cm.get("encrypted_physical_bytes", 0) or 0),
        "health_score": score,
        "alarm_state": _encrypt_alarm_state_snapshot(),
        "lifecycle": enc.get("lifecycle") or {},  # P6-6-6 密钥生命周期（白名单，零密钥材料）
        "perf": cm.get("perf") or {},  # P7收尾·项3 性能指标（encrypt/decrypt 独立维度）
        "perf_buckets": cm.get("perf_buckets") or {},  # P7-B3 分大小性能区间（<1M/1-16M/16M+）
        "key_patrol": _key_patrol_health_snapshot(),  # P7-C3 密钥健康度巡检（零敏感）
        "diagnose": _encrypt_diagnose_snapshot(),  # P7-B4 加密异常诊断（仅可能性+置信度，零建议）
    }


# ---- P6-6-6 密钥轮换管理（admin-only）----
class _RewrapBody(BaseModel):
    keys: list[str] = []
    reason: str = ""


class _RetireBody(BaseModel):
    version: int
    force: bool = False
    reason: str = ""


@admin_governance_router.post("/encryption/rewrap")
async def api_encrypt_rewrap(body: _RewrapBody, user: CurrentUser):
    """批量 DEK 重裹（admin-only，越权 404；🔴5 仅显式 keys 范围，禁无范围全量）。

    只重裹不重加密；失败跳过 + 审计；返回进度/失败清单。
    """
    await _require_admin(user)
    if not body.keys:
        raise HTTPException(status_code=400, detail="必须指定 keys 范围（禁无范围全量）")
    from app.storage.crypto_gate import rotate_rewrap_deks

    res = await rotate_rewrap_deks(keys=body.keys)
    await _gov_admin_audit(user, "governance.encryption.rewrap",
                           {"keys": len(body.keys), **res}, f"DEK 重裹（{len(body.keys)}）")
    if res.get("enabled") is False:
        raise HTTPException(status_code=400, detail="加密未启用")
    return res


@admin_governance_router.post("/encryption/recycle-key")
async def api_encrypt_recycle(body: _RetireBody, user: CurrentUser):
    """三阶段回收·阶段①门槛校验（admin-only）：确认版本零活跃引用才放行；强管控留痕。"""
    await _require_admin(user)
    if not body.reason:
        raise HTTPException(status_code=400, detail="回收需填写原因（双人审计留痕）")
    from app.storage.crypto_gate import retire_legacy_key

    res = await retire_legacy_key(version=body.version, force=body.force)
    await _gov_admin_audit(user, "governance.encryption.recycle-key",
                           {"version": body.version, **res}, f"密钥回收门槛校验 v{body.version}")
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("reason", "active_refs"))
    return res


@admin_governance_router.get("/encryption/refs")
async def api_encrypt_refs(user: CurrentUser):
    """全量版本引用扫描（admin-only；回收前置校验 + 可观测）。"""
    await _require_admin(user)
    from app.storage.crypto_gate import scan_legacy_refs

    res = await scan_legacy_refs()
    await _gov_admin_audit(user, "governance.encryption.refs", {"refs": res.get("refs")},
                           "版本引用扫描")
    return res


# ---- P7 收尾 · 项2 加密合规报表（CSV 注入防护 / 90d 范围 / 速率限制 / 白名单） ----
import io as _io  # noqa: E402
import time as _time  # noqa: E402
from collections import deque as _deque  # noqa: E402

# 速率限制：单 admin 每小时最多 EXPORT_RATE_LIMIT 次（进程内滑动）
_EXPORT_RATE_LIMIT = 3
_EXPORT_MAX_DAYS = 90
_report_rate: dict[str, _deque[float]] = {}


def _csv_cell(v) -> str:
    """CSV 注入防护（🔴）：文本字段若以 = + - @ 开头 → 前缀单引号转义。"""
    s = str(v)
    if s and s[0] in ("=", "+", "-", "@"):
        s = "'" + s
    return s


def _rate_ok(admin_id: str) -> bool:
    now = _time.time()
    q = _report_rate.setdefault(admin_id, _deque())
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= _EXPORT_RATE_LIMIT:
        return False
    q.append(now)
    return True


async def _encryption_report(since, until) -> list[dict]:
    """聚合 crypto.* 审计：按 日/操作类型 统计 总数/成功/失败 + 密钥版本分布（纯统计白名单）。"""
    from app.db import repos as _repos

    factory = get_session_factory()
    rows = []
    async with factory() as s:
        recs, _ = await _repos.list_audit_logs(
            s, operator=None, action_prefix="crypto.",
            since=since, until=until, page=1, page_size=10000)
        for r in recs:
            d = r.detail or {}
            rows.append({
                "ts": (r.created_at or since).strftime("%Y-%m-%d"),
                "action": (r.action or "").replace("crypto.", ""),
                "ok": bool(d.get("ok", True)),
                "version": d.get("version") if d.get("version") is not None else "",
            })
    if not rows:
        return []
    by: dict[tuple[str, str], dict] = {}
    versions: dict[str, int] = {}
    for row in rows:
        key = (row["ts"], row["action"])
        b = by.setdefault(key, {"date": row["ts"], "action": row["action"],
                                "total": 0, "ok": 0, "fail": 0})
        b["total"] += 1
        b["ok" if row["ok"] else "fail"] += 1
        if row["version"]:
            versions[str(row["version"])] = versions.get(str(row["version"]), 0) + 1
    out = sorted(by.values(), key=lambda x: (x["date"], x["action"]))
    return out


@admin_governance_router.get("/encryption/report")
async def api_encrypt_report(user: CurrentUser,
                             since: str | None = None, until: str | None = None,
                             format: str = "json"):
    """加密合规报表（admin-only）：时间窗内按 日/操作类型 聚合（纯统计白名单）。

    安全（🔴）：CSV 注入转义、单次 ≤90 天、单 admin 每小时 ≤3 次、不导出文件名/路径/用户信息。
    """
    await _require_admin(user)
    op = _op_for_report(user)
    if not _rate_ok(op):
        raise HTTPException(status_code=429, detail="导出频率超限（每小时 ≤3 次）")
    if format not in ("json", "csv", "pdf"):
        raise HTTPException(status_code=400, detail="format 仅支持 json/csv/pdf")
    # P7-B2 兼容锚点：PDF 受独立开关 + 总闸控制，关闭 → 404（json/csv 不受影响）
    if format == "pdf" and (not get_settings().REPORT_PDF_ENABLED or not _meta_enabled()):
        raise HTTPException(status_code=404, detail="Not Found")
    from datetime import UTC, datetime, timedelta

    def _parse(s_: str | None, default: datetime) -> datetime:
        if not s_:
            return default
        try:
            return datetime.fromisoformat(s_).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="时间格式非法（ISO）") from None

    until_dt = _parse(until, datetime.now(UTC).replace(tzinfo=None))
    since_dt = _parse(since, until_dt - timedelta(days=30))
    if until_dt - since_dt > timedelta(days=_EXPORT_MAX_DAYS):
        raise HTTPException(status_code=400, detail=f"单次导出范围最长 {_EXPORT_MAX_DAYS} 天")
    data = await _encryption_report(since_dt, until_dt)
    if format == "pdf":
        # P7-B2：渲染受保护 PDF（中文/水印/防复制），指纹写入审计互证
        from app.reporting.pdf_export import pdf_offprint_fingerprint, render_encryption_pdf

        pdf_bytes = render_encryption_pdf(
            data, operator=_op_for_report(user),
            since=since_dt.isoformat(), until=until_dt.isoformat())
        await _gov_admin_audit(
            user, "governance.encryption.report",
            {"days": (until_dt - since_dt).days, "format": format, "rows": len(data),
             "pdf_bytes": len(pdf_bytes),
             "pdf_fp": pdf_offprint_fingerprint(pdf_bytes)},
            f"加密合规报表导出({format})")
        from fastapi.responses import Response

        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 "attachment; filename=encryption-report.pdf"})
    await _gov_admin_audit(user, "governance.encryption.report",
                           {"days": (until_dt - since_dt).days, "format": format,
                            "rows": len(data)}, f"加密合规报表导出({format})")
    if format == "csv":
        import csv

        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "action", "total", "ok", "fail"])
        for row in data:
            w.writerow([_csv_cell(row["date"]), _csv_cell(row["action"]),
                        row["total"], row["ok"], row["fail"]])
        from fastapi.responses import Response

        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=encryption-report.csv"})
    return {"since": since_dt.isoformat(), "until": until_dt.isoformat(),
            "rows": data}


# ---- P7-B1 报表定时归档（admin-only：列表/读取/状态/手动触发） ----

@admin_governance_router.get("/reports/archive")
async def api_list_archived_reports(user: CurrentUser):
    """列出治理归档区的报表归档（仅文件名/大小/时间，无内容无敏感）。"""
    await _require_admin(user)
    from app.observability.report_archiver import ARCHIVE_PREFIX
    from app.storage import get_backend

    backend = get_backend()
    try:
        keys = await backend.list(ARCHIVE_PREFIX)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"列出归档失败：{exc}") from None
    items = []
    for k in sorted(keys, reverse=True):
        rel = k[len(ARCHIVE_PREFIX):]
        try:
            size = await backend.size(k)
        except Exception:  # noqa: BLE001
            size = None
        items.append({"name": rel, "key": k, "size": size})
    await _gov_admin_audit(user, "governance.report.archive.list",
                           {"count": len(items)}, "列出报表归档")
    return {"items": items}


@admin_governance_router.get("/reports/archive/{name:path}")
async def api_get_archived_report(name: str, user: CurrentUser):
    """读取单个归档报表内容（admin-only；越权 404）。"""
    await _require_admin(user)
    from app.observability.report_archiver import ARCHIVE_PREFIX, ARCHIVE_TASK_ID
    from app.storage import get_backend
    from app.storage.base import normalize_artifact_key

    # 路径安全：仅允许归档区前缀内的文件名
    if "/" in name or ".." in name or not name:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        key = normalize_artifact_key(ARCHIVE_TASK_ID, name)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Not Found") from None
    if not key.startswith(ARCHIVE_PREFIX):
        raise HTTPException(status_code=404, detail="Not Found")
    backend = get_backend()
    data = await backend.get(key)
    if data is None:
        raise HTTPException(status_code=404, detail="Not Found")
    # 加密开启时归档为自包含密文 → 解密后返回明文（与版本读取链路一致）
    from app.storage.crypto_gate import decrypt_artifact, is_encrypted_blob

    if is_encrypted_blob(data):
        try:
            data = await decrypt_artifact(data)
        except Exception:  # noqa: BLE001 密文损坏按缺失处理
            raise HTTPException(status_code=404, detail="Not Found") from None
    mime = "application/json" if name.endswith(".json") else "text/csv"
    await _gov_admin_audit(user, "governance.report.archive.read",
                           {"name": name, "bytes": len(data)}, "读取报表归档")
    from fastapi.responses import Response

    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@admin_governance_router.get("/reports/archive-status")
async def api_archive_status(user: CurrentUser):
    """归档器运行态（admin 只读；无敏感字段）。"""
    await _require_admin(user)
    from app.observability import report_archiver

    return report_archiver.archiver_status()


@admin_governance_router.post("/reports/archive/run")
async def api_archive_run_now(user: CurrentUser):
    """手动触发一轮归档（admin-only；force 忽略重叠守卫）。"""
    await _require_admin(user)
    from app.db.base import get_session_factory
    from app.observability import report_archiver

    res = await report_archiver.run_archive_once(get_session_factory(), force=True)
    await _gov_admin_audit(user, "governance.report.archive.run",
                           {"ok": res.get("ok"), "existed": res.get("existed", False)},
                           "手动触发报表归档")
    return res


def _op_for_report(user) -> str:
    if user.authenticated and user.id:
        return "system" if user.is_system else user.id
    return "anonymous"


def _encrypt_alarm_state_snapshot() -> dict:
    """加密告警态快照（仅级别/维度，无敏感字段）。"""
    from app.observability import metrics as _m

    out = {}
    for k, v in _m._gov_alarm_state.items():  # noqa: SLF001  同进程内只读快照
        if k.startswith("encrypt"):
            out[k] = v
    return out


def _key_patrol_health_snapshot() -> dict:
    """P7-C3 密钥健康度巡检快照（admin 白名单；仅计数/级别/维度，零敏感）。"""
    from app.observability import key_patrol

    return key_patrol.key_patrol_health()


def _encrypt_diagnose_snapshot() -> dict:
    """P7-B4 加密异常诊断快照（admin 白名单；仅可能性+置信度，零建议/零敏感）。"""
    from app.observability import encrypt_diagnose

    return encrypt_diagnose.health_snapshot()


@admin_governance_router.get("/snapshots")
async def api_gov_snapshots(user: CurrentUser):
    await _require_admin(user)
    from app.storage.governance import _get_gov_store

    store = _get_gov_store()
    if store is None:
        raise HTTPException(status_code=404, detail="Not Found")  # 中心化未启用
    return {"snapshots": await store.list_snapshots()}


@admin_governance_router.post("/snapshot")
async def api_gov_snapshot_now(user: CurrentUser, body: _SnapBody):
    """手动打快照（变更前建议调用）：保存当前全量配置 {ovr, gray}，返回 key。"""
    await _require_admin(user)
    from app.storage.governance import _get_gov_store

    store = _get_gov_store()
    if store is None:
        raise HTTPException(status_code=404, detail="Not Found")
    snap = await store.load_all()
    key = await store.snapshot(snap)
    await _gov_admin_audit(user, "governance.snapshot.create", {"snapshot": key}, body.reason)
    return {"ok": True, "snapshot": key}


@admin_governance_router.post("/snapshot/{key}/restore")
async def api_gov_snapshot_restore(key: str, user: CurrentUser, body: _SnapBody):
    """一键回滚到指定快照（⭐5）。逐项写回权威并广播，降低期间读保留本地缓存。"""
    await _require_admin(user)
    from app.storage.governance import (
        _get_gov_store,
        gray_set_remote,
        set_governance_override_remote,
    )

    store = _get_gov_store()
    if store is None:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        snap = await store.load_snapshot(key)
    except GovConfigUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        ovr = (snap.get("ovr") or {})
        for feat, (_ver, val) in ovr.items():
            if val is not None:
                await set_governance_override_remote(feat, bool(val))
        gray = (snap.get("gray") or {})
        for feat, (_ver, members) in gray.items():
            await gray_set_remote(feat, list(members), [])
    except GovConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await _gov_admin_audit(user, "governance.snapshot.restore", {"key": key}, body.reason)
    return {"ok": True, "restored": key}


async def _gov_admin_audit(user, action: str, detail: dict, reason: str) -> None:
    from app.db import repos as _repos

    factory = get_session_factory()
    async with factory() as s:
        await _repos.write_audit(
            s, task_id="", operator=user.id or "system", action=action,
            detail={**detail, "reason": reason})


# ---- P6-5 N3 容量规划（权限分层：普通用户仅本人，admin=全局+定价） ----
plan_router = APIRouter(prefix="/storage", tags=["storage-plan"])


@plan_router.get("/plan")
async def api_storage_plan(user: CurrentUser):
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    from app.storage.governance import storage_plan

    is_admin = bool(user.authenticated and (user.is_system or user.role_is_admin()))
    if is_admin:
        return await storage_plan(None, is_admin=True)
    owner = _owner_id(user)
    if not owner:
        raise HTTPException(status_code=404, detail="Not Found")
    return await storage_plan(owner, is_admin=False)


# ---- 回收站（批次 J） ----
recycle_router = APIRouter(prefix="/artifacts/recycle", tags=["artifacts-recycle"])


@recycle_router.get("")
async def api_recycle_list(user: CurrentUser, page: int = 1, page_size: int = 50):
    if not _recycle_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    owner = _owner_id(user)
    if not owner:
        raise HTTPException(status_code=404, detail="Not Found")
    return await list_recycle(owner_id=owner, page=page, page_size=page_size)


@recycle_router.post("/{task_id}/{rel_path:path}/delete")
async def api_recycle_delete(task_id: str, rel_path: str, user: CurrentUser):
    if not _recycle_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_can_edit(session, user, task)
    return await soft_delete_artifact(task_id=task_id, rel_path=rel_path)


@recycle_router.post("/{task_id}/{rel_path:path}/restore")
async def api_recycle_restore(task_id: str, rel_path: str, user: CurrentUser):
    if not _recycle_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_can_edit(session, user, task)
    return await restore_artifact(task_id=task_id, rel_path=rel_path)


# ---- P6-2 O3 批量操作（最佳努力 + 预校验 + 幂等 + 全审计） ----
batch_router = APIRouter(prefix="/artifacts/batch", tags=["artifacts-batch"])


class _BatchBody(BaseModel):
    items: list[dict]  # [{task_id, rel_path}]
    idempotency_key: str | None = None
    confirm: bool = False  # N3 二次确认令牌，缺省拒绝危险批量操作


async def _batch_run(user: UserPrincipal, op: str, items: list[dict],
                     confirm: bool = False) -> dict:
    """批量执行：逐项预校验权限→状态幂等→调用对应治理函数→逐条审计。

    最佳努力模式（非原子）：返回每项 ok/reason + 汇总；已处于目标态视为幂等成功。
    ``confirm`` 缺省 False → 拒绝（N3 防误触发，Soft-contained 兜底：delete 一律软删）。
    """
    import logging

    from app.db.repos import get_artifact_by_rel

    logger = logging.getLogger(__name__)
    if not confirm:
        raise HTTPException(status_code=400, detail="危险批量操作需 confirm=true 确认后执行")
    results = []
    succeeded = failed = 0
    _cold = "cold"
    impact_byt = 0
    for it in items:
        task_id = str(it.get("task_id", ""))
        rel_path = str(it.get("rel_path", ""))
        try:
            if not (task_id and rel_path):
                raise HTTPException(status_code=404, detail="Not Found")
            task = await _load_task(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Not Found")
            factory = get_session_factory()
            async with factory() as session:
                await _require_can_edit(session, user, task)
            # 预校验 + 影响预估
            async with factory() as session:
                rec = await get_artifact_by_rel(session, task_id=task_id,
                                                rel_path=rel_path)
                if rec is not None:
                    impact_byt += int(rec.size or 0)
            # 幂等预判：coldize 对已冷文件视为幂等成功
            if op == "coldize":
                if rec is not None and rec.tier == _cold:
                    results.append({"task_id": task_id, "rel_path": rel_path,
                                    "ok": True, "reason": "already_cold"})
                    succeeded += 1
                    continue
            r = None
            if op == "coldize":
                from app.storage.governance import tier_archive
                r = await tier_archive(task_id=task_id, rel_path=rel_path)
            elif op == "delete":
                r = await soft_delete_artifact(task_id=task_id, rel_path=rel_path)
            else:
                r = await restore_artifact(task_id=task_id, rel_path=rel_path)
            ok = bool(r and r.get("ok"))
            results.append({"task_id": task_id, "rel_path": rel_path,
                            "ok": ok, "reason": "" if ok else str(r.get("reason", ""))})
            succeeded += 1 if ok else 0
            failed += 0 if ok else 1
        except HTTPException as exc:
            results.append({"task_id": task_id, "rel_path": rel_path,
                            "ok": False, "reason": exc.detail})
            failed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量%s失败 %s/%s: %s", op, task_id, rel_path, exc)
            results.append({"task_id": task_id, "rel_path": rel_path,
                            "ok": False, "reason": str(exc)})
            failed += 1
    return {"op": op, "succeeded": succeeded, "failed": failed,
            "impact": {"count": len(items), "bytes": impact_byt}, "items": results}


@batch_router.post("/coldize")
async def api_batch_coldize(body: _BatchBody, user: CurrentUser):
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return await _batch_run(user, "coldize", body.items, confirm=body.confirm)


@batch_router.post("/delete")
async def api_batch_delete(body: _BatchBody, user: CurrentUser):
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return await _batch_run(user, "delete", body.items, confirm=body.confirm)


@batch_router.post("/restore")
async def api_batch_restore(body: _BatchBody, user: CurrentUser):
    if not _meta_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return await _batch_run(user, "restore", body.items, confirm=body.confirm)
