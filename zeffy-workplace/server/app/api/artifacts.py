"""P5 产物读取 API：/artifacts（鉴权闭环，🔴4）+ P5-1 版本管理接口（🔴/⭐ 修订版）。

- ``GET    /artifacts/{task_id}/{path}``            —— 读最新 → can_view
- ``GET    /artifacts/{task_id}/{path}?version=N``  —— 读历史版本 → can_view
- ``GET    /artifacts/{task_id}``                   —— 列表 → can_view
- ``GET    /artifacts/{task_id}/_versions``         —— 版本列表(分页) → can_view
- ``GET    /artifacts/{task_id}/_diff``             —— 版本 diff → can_view
- ``DELETE /artifacts/{task_id}/{path}``            —— 删主 key(级联版本) → can_edit
- ``DELETE /artifacts/{task_id}/{path}?version=N``  —— 删单版本 → can_edit
- 越权/不存在统一 404（防资源枚举）；版本接口在 ``ARTIFACT_VERSIONS_ENABLED=false`` 时 404。
- 审计 action 细化：artifact_get/artifact_list/artifact_delete/artifact_version_get/
  artifact_version_list/artifact_version_diff/artifact_version_delete（⭐3，带 task_id+trace_id+version）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.storage.versioning import VersionManager

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


def _vm() -> VersionManager | None:
    """版本能力：仅版本开启（get_backend 返回 VersionManager 且 enabled）时可用。"""
    backend = get_backend()
    return backend if isinstance(backend, VersionManager) and backend.enabled else None


def _rel_of(key: str) -> str:
    return key[len("artifacts/"):].split("/", 1)[1]


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


@router.get("/{task_id}/_versions")
async def list_artifact_versions(task_id: str, user: CurrentUser,
                                 path: str = Query(...),
                                 page: int = Query(default=1, ge=1),
                                 page_size: int = Query(default=50, ge=1, le=200)) -> dict:
    """版本列表（分页 + 总数 + 总字节，⭐2）。"""
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    vm = _vm()
    if vm is None:
        raise HTTPException(status_code=404, detail="Not Found")
    key = _route_key(task_id, path)  # 校验 path 合法性
    factory = get_session_factory()
    async with factory() as session:
        await _require(session, user, task, "view")
        data = await vm.list_versions(task_id, _rel_of(key), page=page, page_size=page_size)
        await write_audit(session, task_id=task_id, operator=_op(user),
                          action="artifact_version_list",
                          detail={"key": key, "page": page, "total": data["total"]})
    return {"task_id": task_id, "path": path, **data}


@router.get("/{task_id}/_diff")
async def diff_artifact_versions(task_id: str, user: CurrentUser,
                                 path: str = Query(...),
                                 from_v: int = Query(..., ge=1),
                                 to_v: int = Query(..., ge=1)) -> dict:
    """版本行级 diff（🔴6 资源约束：大小/二进制/预览截断）。"""
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    vm = _vm()
    if vm is None:
        raise HTTPException(status_code=404, detail="Not Found")
    key = _route_key(task_id, path)
    factory = get_session_factory()
    async with factory() as session:
        await _require(session, user, task, "view")
        data = await vm.diff_versions(task_id, _rel_of(key), from_v, to_v)
        await write_audit(session, task_id=task_id, operator=_op(user),
                          action="artifact_version_diff",
                          detail={"key": key, "from": from_v, "to": to_v,
                                  "result": data.get("status")})
    return data


@router.get("/{task_id}/{path:path}")
async def get_artifact(task_id: str, path: str, user: CurrentUser,
                       version: int | None = Query(default=None, ge=1)):
    """读产物：鉴权 → 返回文件流（或 S3 短时直链 302）。``?version=N`` 读历史版本。"""
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    key = _route_key(task_id, path)
    factory = get_session_factory()
    backend = get_backend()
    if version is not None:
        vm = _vm()
        if vm is None:
            raise HTTPException(status_code=404, detail="Not Found")
        async with factory() as session:
            await _require(session, user, task, "view")
            data = await vm.get_version_bytes(task_id, _rel_of(key), version)
        if data is None:
            raise HTTPException(status_code=404, detail="Not Found")
        async with factory() as session:
            await write_audit(session, task_id=task_id, operator=_op(user),
                              action="artifact_version_get",
                              detail={"key": key, "version": version})
        record("get", backend=backend.name)
        return StreamingResponse(iter([data]), media_type=guess_mime(path),
                                 headers={"X-Artifact-Key": key, "X-Version": str(version)})
    async with factory() as session:
        await _require(session, user, task, "view")
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
async def delete_artifact(task_id: str, path: str, user: CurrentUser,
                          version: int | None = Query(default=None, ge=1)) -> dict:
    """删产物（写操作 → can_edit）。``?version=N`` 删单版本；否则删主 key 并级联删全部版本。"""
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    key = _route_key(task_id, path)
    ensure_artifact_key(key)
    factory = get_session_factory()
    backend = get_backend()
    async with factory() as session:
        await _require(session, user, task, "edit")
        if version is not None:
            vm = _vm()
            if vm is None:
                raise HTTPException(status_code=404, detail="Not Found")
            ok = await vm.delete_version(task_id, _rel_of(key), version)
            if not ok:
                raise HTTPException(status_code=404, detail="Not Found")
            await write_audit(session, task_id=task_id, operator=_op(user),
                              action="artifact_version_delete",
                              detail={"key": key, "version": version})
            record("delete", backend=backend.name)
            return {"ok": True, "key": key, "version": version}
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
