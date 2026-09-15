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

import contextlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse

from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db.base import get_session_factory
from app.db.repos import get_task, write_audit
from app.storage import get_backend, record
from app.storage.base import (
    RangeNotSatisfiableError,
    SecurityError,
    StorageError,
    ensure_artifact_key,
    guess_mime,
    normalize_artifact_key,
)
from app.storage.versioning import VersionManager

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


def _artifact_etag(backend_name: str, size: int | None,
                   fingerprint: str | None = None) -> str:
    """🔴2 ETag：优先后端强指纹（Local size+mtime_ns / S3 服务端 ETag），
    同大小内容变更也能被检出；无指纹时降级为 size 弱校验。"""
    base = size if size is not None else "x"
    return f'"{backend_name}-{base}:{fingerprint}"' if fingerprint else f'"{backend_name}-{base}"'


def _range_bytes(header: str) -> tuple[int, int | None] | None:
    """解析 Range: bytes=start-end / bytes=start-。非法/多段/后缀 → None（调用方降级 200）。
    返回 (start, end|None)。"""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].strip()
    if "," in spec:  # 多段不支持 → 降级 200
        return None
    if "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    if start_s == "" or not start_s.isdigit():  # 后缀 bytes=-N 或非数字 → 降级
        return None
    start = int(start_s)
    end = int(end_s) if end_s.isdigit() else None
    return start, end


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


@router.head("/{task_id}/{path:path}")
async def head_artifact(task_id: str, path: str, user: CurrentUser,
                        version: int | None = Query(default=None, ge=1)) -> Response:
    """P5-4 HEAD：返回 size + Accept-Ranges + ETag（鉴权同 GET；无 body）。"""
    task = await _load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    key = _route_key(task_id, path)
    factory = get_session_factory()
    backend = get_backend()
    size = None
    if version is not None:
        vm = _vm()
        if vm is None:
            raise HTTPException(status_code=404, detail="Not Found")
        async with factory() as session:
            await _require(session, user, task, "view")
            meta = await vm.get_version_meta(task_id, _rel_of(key), version)
            if meta is None:
                raise HTTPException(status_code=404, detail="Not Found")
            size = meta.get("size")
    else:
        async with factory() as session:
            await _require(session, user, task, "view")
            size = await backend.size(key)
        if size is None and not await backend.exists(key):
            raise HTTPException(status_code=404, detail="Not Found")
    fingerprint = await backend.fingerprint(key) if size is not None else None
    headers = {
        "Accept-Ranges": "bytes" if get_settings().RANGE_ENABLED else "none",
        "ETag": _artifact_etag(backend.name, size, fingerprint),
        "X-Artifact-Key": key,
    }
    if size is not None:
        headers["Content-Length"] = str(size)
    async with factory() as session:
        await write_audit(session, task_id=task_id, operator=_op(user),
                          action="artifact_head", detail={"key": key, "size": size})
    return Response(status_code=200, headers=headers)


@router.get("/{task_id}/{path:path}")
async def get_artifact(request: Request, task_id: str, path: str, user: CurrentUser,
                       version: int | None = Query(default=None, ge=1)):
    """读产物：鉴权 → 返回文件流（或 S3 短时直链 302）。``?version=N`` 读历史版本。
    P5-4：RANGE_ENABLED 时支持 ``Range: bytes=start[-end]`` → 206 + Content-Range（🔴）。"""
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
            url = f"{get_settings().ST_ARTIFACT_PUBLIC_BASE.rstrip('/')}/{key}"
            await write_audit(session, task_id=task_id, operator=_op(user),
                              action="artifact_get", detail={"key": key, "mode": "redirect"})
            record("get", backend=backend.name)
            return RedirectResponse(url=url)
        if not await backend.exists(key):
            raise HTTPException(status_code=404, detail="Not Found")

        # P7 收尾 · 流式解密：单流探测首块判密文，密文则流式解（内存常量级），
        # 非密文原样透传（零漂移）。仅加密开启时启用本分支。
        if get_settings().ARTIFACT_META_ENABLED and get_settings().ARTIFACT_ENCRYPT_ENABLED:
            from app.storage.crypto_gate import (
                C as _C,
            )
            from app.storage.crypto_gate import (
                _decrypt_stream_rest,
                is_encrypted_blob,
                peek_plain_size,
            )

            _s = backend.stream(key)
            _first = b""
            async for _c in _s:
                _first = _c
                break
            if not _first:
                raise HTTPException(status_code=404, detail="Not Found")
            if is_encrypted_blob(_first):
                rh = request.headers.get("range")
                rmd = _range_bytes(rh) if get_settings().RANGE_ENABLED and rh else None
                if rmd is not None:
                    plen = peek_plain_size(_first)
                    if plen is None:
                        raise HTTPException(status_code=400,
                                            detail="加密文件元数据损坏")
                    start, end = rmd
                    if start >= plen:
                        raise HTTPException(
                            status_code=416,
                            headers={"Content-Range": f"bytes */{plen}"},
                            detail="Range 越界")
                    end = min(end if end is not None else plen - 1, plen - 1)
                    length = end - start + 1

                    async def _range_enc(_s=_s, _first=_first, _start=start, _end=end):
                        try:
                            async for pt in _decrypt_stream_rest(
                                    _s, head=_first, start=_start, end=_end + 1,
                                    task_id=task_id, owner_id=user.id):
                                yield pt
                        except _C.EncryptError as exc:
                            raise HTTPException(
                                status_code=400,
                                detail="解密失败（密文损坏或密钥不符）") from exc

                    await write_audit(session, task_id=task_id, operator=_op(user),
                                      action="artifact_get",
                                      detail={"key": key, "mode": "range-enc",
                                              "start": start, "end": end})
                    record("get", backend=backend.name)
                    return StreamingResponse(
                        _range_enc(), media_type=guess_mime(path), status_code=206,
                        headers={
                            "Content-Range": f"bytes {start}-{end}/{plen}",
                            "Content-Length": str(length),
                            "Accept-Ranges": "bytes",
                            "X-Artifact-Key": key,
                        })

                async def _stream_dec(_s=_s, _first=_first):
                    try:
                        async for pt in _decrypt_stream_rest(
                                _s, head=_first, task_id=task_id, owner_id=user.id):
                            yield pt
                    except _C.EncryptError as exc:
                        raise HTTPException(
                            status_code=400,
                            detail="解密失败（密文损坏或密钥不符）") from exc

                await write_audit(session, task_id=task_id, operator=_op(user),
                                  action="artifact_get",
                                  detail={"key": key, "mode": "decrypt"})
                record("get", backend=backend.name)
                return StreamingResponse(_stream_dec(),
                                         media_type=guess_mime(path),
                                         headers={"X-Artifact-Key": key})
            # 非密文 → 回落普通路径（其会重新开流从头读，零漂移）；先关闭探测流
            await _s.aclose()

        size = None
        range_md = None
        # 🔴3 Range 解析（仅 RANGE_ENABLED）：非法/多段/后缀 → 降级 200 全量
        if get_settings().RANGE_ENABLED:
            rh = request.headers.get("range")
            if rh:
                range_md = _range_bytes(rh)
        if range_md is not None:
            size = await backend.size(key)
            if size is None:
                range_md = None  # 拿不到 size → 降级 200
            else:
                start, end = range_md
                if start >= size:
                    raise HTTPException(status_code=416,
                                        headers={"Content-Range": f"bytes */{size}"},
                                        detail="Range 越界")
                end = min(end if end is not None else size - 1, size - 1)
                length = end - start + 1
                fingerprint = await backend.fingerprint(key)

                async def _range_stream():
                    remaining = length
                    try:
                        async for chunk in backend.stream(key, start=start):
                            if remaining <= 0:
                                break
                            if len(chunk) > remaining:
                                chunk = chunk[:remaining]
                            remaining -= len(chunk)
                            yield chunk
                    except RangeNotSatisfiableError:
                        return

                await write_audit(session, task_id=task_id, operator=_op(user),
                                  action="artifact_get", detail={"key": key, "mode": "range", "start": start, "end": end})
                record("get", backend=backend.name)
                return StreamingResponse(
                    _range_stream(), media_type=guess_mime(path),
                    status_code=206,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{size}",
                        "Content-Length": str(length),
                        "Accept-Ranges": "bytes",
                        "ETag": _artifact_etag(backend.name, size, fingerprint),
                        "X-Artifact-Key": key,
                    })

        async def _stream():
            async for chunk in backend.stream(key):
                yield chunk

        fp = await backend.fingerprint(key)
        await write_audit(session, task_id=task_id, operator=_op(user),
                          action="artifact_get", detail={"key": key, "mode": "stream"})
        # P6-2 O1 热度埋点：仅完整文件读取（非 Range）触发；分层关闭时 governance 内 no-op
        if get_settings().ARTIFACT_META_ENABLED:
            with contextlib.suppress(Exception):  # noqa: BLE001
                from app.storage.governance import touch_artifact

                await touch_artifact(
                    task_id=task_id, rel_path=_rel_of(key))
    record("get", backend=backend.name)
    return StreamingResponse(_stream(), media_type=guess_mime(path),
                             headers={"X-Artifact-Key": key,
                                      "ETag": _artifact_etag(backend.name, size, fp),
                                      "Accept-Ranges": "bytes" if get_settings().RANGE_ENABLED else "none"})


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
