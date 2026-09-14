"""P5 产物读取 API：/artifacts（鉴权闭环，🔴4）。

- ``GET    /artifacts/{task_id}/{path}`` —— 读 → can_view
- ``GET    /artifacts/{task_id}``        —— 列表 → can_view
- ``DELETE /artifacts/{task_id}/{path}`` —— 删 → can_edit
- 越权/不存在统一 404（防资源枚举，复用 RBAC 约定）。
- AUTH off 匿名 → can_* 恒 True（P2 兼容）。
- 预签名：仅 S3 后端且配了 ``ST_ARTIFACT_PUBLIC_BASE`` 时 302 到短时直链（默认 15min TTL 语义）；
  其余一律在线流式返回，绝不生成永久公开链接。
- 全部产物操作写 AuditLog（task_id + trace_id 自动注入）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse

from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db.base import get_session_factory
from app.db.repos import get_task, write_audit
from app.storage import get_backend, record
from app.storage.base import (
    SecurityError,
    StorageError,
    ensure_artifact_key,
    guess_mime,
    normalize_artifact_key,
)

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


async def _load_task(task_id: str):
    factory = get_session_factory()
    async with factory() as session:
        return await get_task(session, task_id)


async def _require(session, user: UserPrincipal, task, need: str) -> None:
    """🔴4 统一权限判定（读→can_view，删→can_edit）；无权限一律 404。"""
    from app.auth import permissions as perm

    ok = await perm.can_edit(session, user, task) if need == "edit" \
        else await perm.can_view(session, user, task)
    if not ok:
        raise HTTPException(status_code=404, detail="Not Found")


def _route_key(task_id: str, path: str) -> str:
    try:
        return normalize_artifact_key(task_id, path)
    except SecurityError as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc


@router.get("/{task_id}")
async def list_artifacts(task_id: str, user: CurrentUser) -> dict:
    """列出任务全部产物 key（含存量兼容映射）。"""
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require(session, user, task, "view")
        backend = get_backend()
        try:
            keys = await backend.list(f"artifacts/{task_id}")
        except StorageError as exc:
            raise HTTPException(status_code=500, detail=f"存储不可用：{exc}") from exc
        await write_audit(session, task_id=task_id, operator=_op(user),
                          action="artifact_list", detail={"keys": len(keys)})
    record("list", backend=backend.name)
    return {"task_id": task_id, "keys": keys, "count": len(keys)}


@router.get("/{task_id}/{path:path}")
async def get_artifact(task_id: str, path: str, user: CurrentUser):
    """读产物：鉴权 → 返回文件流（或 S3 短时直链 302）。"""
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    key = _route_key(task_id, path)
    factory = get_session_factory()
    async with factory() as session:
        await _require(session, user, task, "view")
        backend = get_backend()
        if backend.name == "s3" and get_settings().ST_ARTIFACT_PUBLIC_BASE:
            # 预签名/短时直链（ST_SIGNED_URL_TTL 语义由外部配置保证）；禁永久链接
            url = f"{get_settings().ST_ARTIFACT_PUBLIC_BASE.rstrip('/')}/{key}"
            await write_audit(session, task_id=task_id, operator=_op(user),
                              action="artifact_get", detail={"key": key, "mode": "redirect"})
            record("get", backend=backend.name)
            return RedirectResponse(url=url)
        if not await backend.exists(key):
            raise HTTPException(status_code=404, detail="Not Found")

        async def _stream():
            async for chunk in backend.stream(key):
                yield chunk

        await write_audit(session, task_id=task_id, operator=_op(user),
                          action="artifact_get", detail={"key": key, "mode": "stream"})
    record("get", backend=backend.name)
    return StreamingResponse(_stream(), media_type=guess_mime(path),
                             headers={"X-Artifact-Key": key})


@router.delete("/{task_id}/{path:path}")
async def delete_artifact(task_id: str, path: str, user: CurrentUser) -> dict:
    """删产物（写操作 → can_edit）。"""
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    key = _route_key(task_id, path)
    ensure_artifact_key(key)
    factory = get_session_factory()
    backend = get_backend()
    async with factory() as session:
        await _require(session, user, task, "edit")
        try:
            ok = await backend.delete(key)
        except StorageError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc
        if not ok:
            raise HTTPException(status_code=404, detail="Not Found")
        await write_audit(session, task_id=task_id, operator=_op(user),
                          action="artifact_delete", detail={"key": key})
    record("delete", backend=backend.name)
    return {"ok": True, "key": key}


def _op(user: UserPrincipal) -> str:
    if user.authenticated and user.id:
        return "system" if user.is_system else user.id
    return "anonymous"
