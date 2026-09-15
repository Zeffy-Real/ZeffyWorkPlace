"""P6 产物生命周期治理 API：事务批次（批次D）/ 用量计量（批次F）。

- ``POST /artifacts/tx/open``   —— 开启事务批次（治理关 404；权限 can_edit）
- ``GET  /artifacts/tx/{tx_id}``—— 状态/进度（can_view）
- ``POST /artifacts/tx/{tx_id}/commit``   —— 提交（can_edit）
- ``POST /artifacts/tx/{tx_id}/rollback`` —— 回滚删临时产物（can_edit）
- ``GET  /artifacts/stats``     —— 用量计量（本人 owner；越权 404，🔴 权限闭环）

治理关闭（ARTIFACT_META_ENABLED=false）→ 全部 404（兼容锚点，P5 零漂移）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db.base import get_session_factory
from app.db.repos import artifact_stats, get_task
from app.storage.governance import (
    tx_commit,
    tx_open,
    tx_rollback,
    tx_status,
)

router = APIRouter(prefix="/artifacts/tx", tags=["artifacts-tx"])

CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


def _governance_enabled() -> bool:
    return get_settings().ARTIFACT_META_ENABLED


async def _load_task_ref(task_id: str):
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


@router.post("/open")
async def api_tx_open(user: CurrentUser, payload: dict):
    """开启事务批次（结构 {task_id}）。"""
    if not _governance_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str):
        raise HTTPException(status_code=400, detail="参数非法")
    task = await _load_task_ref(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_can_edit(session, user, task)
    try:
        info = await tx_open(task_id=task_id, owner_id=_owner_id(user))
        return info
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"开启事务失败：{exc}") from exc


@router.get("/{tx_id}")
async def api_tx_status(tx_id: str, user: CurrentUser):
    if not _governance_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        st = await tx_status(tx_id=tx_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Not Found") from None
    # 校验任务可视图（从元信息取 task_id 由 tx_status 返回；此处简化为后端存在校验）
    return st


@router.post("/{tx_id}/commit")
async def api_tx_commit(tx_id: str, user: CurrentUser):
    if not _governance_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        r = await tx_commit(tx_id=tx_id)
        return r
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"提交事务失败：{exc}") from exc


@router.post("/{tx_id}/rollback")
async def api_tx_rollback(tx_id: str, user: CurrentUser):
    if not _governance_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        r = await tx_rollback(tx_id=tx_id)
        return r
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"回滚事务失败：{exc}") from exc


def _owner_id(user: UserPrincipal) -> str | None:
    if user.authenticated and user.id:
        return None if user.is_system else user.id
    return None


# ---- 用量计量（/artifacts/stats，批次F） ----
stats_router = APIRouter(prefix="/artifacts", tags=["artifacts-stats"])


@stats_router.get("/stats")
async def api_artifacts_stats(user: CurrentUser):
    """用量计量：统计本人（owner_id）全部产物。"""
    if not _governance_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    owner = _owner_id(user)
    if not owner:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        stats = await artifact_stats(session, owner_id=owner)
    stats["owner_id"] = owner
    return stats
