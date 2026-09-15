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

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db.base import get_session_factory
from app.db.repos import artifact_stats, get_quota_used, get_task
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
    return stats


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
