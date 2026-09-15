"""P5-5 上传断点续传：init → chunk → commit（究极审查修订版，独立链路零侵入）。

- upload_id = 服务端 uuid（客户端不可控路径，🔴1）
- 暂存区 key = artifacts/_upload/{upload_id}/{offset}.part + .meta
- 同 (task_id, rel) 至多一个活跃上传（互斥，🔴4）；init 按签名续传或作废重建
- chunk 只接受连续 offset（🔴3）；块级原子+幂等（🔴1）
- commit 磁盘预检 + 原子落位 + 恒算 sha256（🔴2/🔴5）；last_active 清理不误杀（🔴6）
- UPLOAD_ENABLED=false 时全部 404（兼容锚点，零漂移）
- 独立 router，前缀 /artifacts/upload，在主 router 之前注册避免被 /{task_id}/{path} 抢占。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db.base import get_session_factory
from app.db.repos import get_task, write_audit
from app.storage import get_backend, record
from app.storage.base import (
    FileExistsError_,
    SecurityError,
    StorageError,
    guess_mime,
    normalize_artifact_key,
)

router = APIRouter(prefix="/artifacts/upload", tags=["upload"])

CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


def _op(user: UserPrincipal) -> str:
    if user.authenticated and user.id:
        return "system" if user.is_system else user.id
    return "anonymous"


async def _load_task_ref(task_id: str):
    factory = get_session_factory()
    async with factory() as session:
        return await get_task(session, task_id)


async def _require_edit(session, user, task) -> None:
    from app.auth import permissions as perm

    if not await perm.can_edit(session, user, task):
        raise HTTPException(status_code=404, detail="Not Found")


async def _require_view(session, user, task) -> None:
    from app.auth import permissions as perm

    if not await perm.can_view(session, user, task):
        raise HTTPException(status_code=404, detail="Not Found")


def _upload_chunk_key(upload_id: str, offset: int) -> str:
    return f"artifacts/_upload/{upload_id}/{offset}.part"


def _upload_meta_key(upload_id: str) -> str:
    return f"artifacts/_upload/{upload_id}/.meta"


def _require_upload_enabled() -> None:
    if not get_settings().UPLOAD_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")


async def _read_upload_meta(backend, upload_id: str) -> dict:
    data = await backend.get(_upload_meta_key(upload_id))
    if data is None:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="上传元数据损坏") from exc


async def _upload_next_offset(backend, upload_id: str, size: int, chunk_size: int) -> int:
    """最大连续完整偏移（按 0/8/16... 对齐扫描，遇缺口即停）。"""
    offset = 0
    while offset < size:
        if not await backend.exists(_upload_chunk_key(upload_id, offset)):
            return offset
        offset += chunk_size
    return size


async def _safe_delete(backend, key: str) -> None:
    try:
        await backend.delete(key)
    except StorageError:
        pass


async def _delete_upload(backend, upload_id: str) -> None:
    try:
        keys = await backend.list(f"artifacts/_upload/{upload_id}")
    except StorageError:
        return
    for k in keys:
        await _safe_delete(backend, k)


async def _scan_active_uploads(backend, task_id: str, rel: str) -> list[tuple[str, dict]]:
    try:
        keys = await backend.list("artifacts/_upload")
    except StorageError:
        return []
    found: list[tuple[str, dict]] = []
    for mk in keys:
        if not mk.endswith("/.meta"):
            continue
        data = await backend.get(mk)
        if data is None:
            continue
        try:
            m = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if m.get("task_id") == task_id and m.get("rel") == rel:
            found.append((mk.split("/")[-2], m))
    return found


async def _bump_upload_active(backend, upload_id: str) -> None:
    try:
        meta = await _read_upload_meta(backend, upload_id)
        meta["last_active"] = time.time()
        await backend.put(_upload_meta_key(upload_id), json.dumps(meta).encode("utf-8"),
                          mode="overwrite")
    except StorageError:
        pass


async def _cleanup_stale_uploads(backend) -> None:
    ttl = get_settings().UPLOAD_TTL
    try:
        keys = await backend.list("artifacts/_upload")
    except StorageError:
        return
    for mk in {k for k in keys if k.endswith("/.meta")}:
        try:
            data = await backend.get(mk)
            if data is None:
                continue
            meta = json.loads(data.decode("utf-8"))
            last = meta.get("last_active") or meta.get("created_at") or 0
            if time.time() - last > ttl:
                upload_id = mk.split("/")[-2]
                for k in keys:
                    if k.startswith(f"artifacts/_upload/{upload_id}"):
                        await _safe_delete(backend, k)
        except (ValueError, UnicodeDecodeError, StorageError):
            continue


async def _update_audit(session, task_id, user, action, detail) -> None:
    await write_audit(session, task_id=task_id, operator=_op(user), action=action, detail=detail)


@router.post("/init")
async def upload_init(user: CurrentUser, request: Request):
    """初始化：互斥 + 断点续传；upload_id=服务端 uuid（🔴1/🔴4/🔴3）。"""
    _require_upload_enabled()
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求体非法") from exc
    task_id = payload.get("task_id")
    rel = payload.get("rel")
    size = payload.get("size")
    if not isinstance(task_id, str) or not isinstance(rel, str) or not isinstance(size, int):
        raise HTTPException(status_code=400, detail="参数非法")
    sm = get_settings()
    if size <= 0 or size > sm.UPLOAD_MAX_SIZE:
        raise HTTPException(status_code=400, detail="size 越界")
    try:
        key = normalize_artifact_key(task_id, rel)
    except SecurityError as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc

    task = await _load_task_ref(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_edit(session, user, task)

    backend = get_backend()
    await _cleanup_stale_uploads(backend)
    md5 = payload.get("md5") if isinstance(payload.get("md5"), str) and payload["md5"] else None
    chunk_size = sm.UPLOAD_CHUNK

    existing = await _scan_active_uploads(backend, task_id, rel)
    upload_id: str | None = None
    for _id, m in existing:
        if m.get("size") == size and (m.get("md5") or None) == md5:
            upload_id = _id
            break
    if upload_id is None:
        for _id, _m in existing:
            await _delete_upload(backend, _id)  # 异签名活跃上传作废清理
        upload_id = uuid.uuid4().hex
        meta = {"task_id": task_id, "rel": rel, "size": size, "md5": md5,
                "chunk_size": chunk_size, "key": key, "created_at": time.time(),
                "last_active": time.time()}
        try:
            await backend.put(_upload_meta_key(upload_id), json.dumps(meta).encode("utf-8"),
                              mode="no_overwrite")
        except FileExistsError_:
            pass

    async with factory() as session:
        await _update_audit(session, task_id, user, "artifact_upload_init",
                            {"key": key, "size": size, "upload_id": upload_id})
    record("upload_init", backend=backend.name)
    next_offset = await _upload_next_offset(backend, upload_id, size, chunk_size)
    return {"upload_id": upload_id, "chunk_size": chunk_size,
            "next_offset": next_offset, "done": next_offset >= size}


@router.put("/{upload_id}/chunk")
async def upload_chunk(upload_id: str, user: CurrentUser, request: Request,
                       offset: int = Query(...)):
    """写入一块（连续 offset，块级原子+幂等，🔴1/🔴3）。"""
    _require_upload_enabled()
    backend = get_backend()
    meta = await _read_upload_meta(backend, upload_id)
    task_id = meta["task_id"]
    size = meta["size"]
    chunk_size = meta["chunk_size"]

    task = await _load_task_ref(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_edit(session, user, task)

    if offset < 0 or offset % chunk_size != 0 or offset >= size:
        raise HTTPException(status_code=400, detail="offset 非法或未对齐")
    next_off = await _upload_next_offset(backend, upload_id, size, chunk_size)
    if offset > next_off:
        raise HTTPException(status_code=400, detail=f"非连续块，请从 offset={next_off} 开始")
    data = await request.body()
    if offset + len(data) > size or not data:
        raise HTTPException(status_code=400, detail="块越界或为空")
    chunk_key = _upload_chunk_key(upload_id, offset)
    if await backend.exists(chunk_key):
        cur = await backend.get(chunk_key)
        if cur != data:
            await backend.put(chunk_key, data, mode="overwrite")
    else:
        await backend.put(chunk_key, data, mode="no_overwrite")
    await _bump_upload_active(backend, upload_id)
    async with factory() as session:
        await _update_audit(session, task_id, user, "artifact_upload_chunk",
                            {"upload_id": upload_id, "offset": offset, "bytes": len(data)})
    record("upload_chunk", backend=backend.name)
    received = await _upload_next_offset(backend, upload_id, size, chunk_size)
    return {"received": received}


@router.post("/{upload_id}/commit")
async def upload_commit(upload_id: str, user: CurrentUser):
    """校验完整 + 磁盘预检 + sha256 恒算 → 原子落位主 key → 清暂存（🔴2/🔴5）。"""
    _require_upload_enabled()
    backend = get_backend()
    meta = await _read_upload_meta(backend, upload_id)
    task_id = meta["task_id"]
    rel = meta["rel"]
    key = meta["key"]
    size = meta["size"]
    chunk_size = meta["chunk_size"]
    expect_md5 = meta.get("md5")

    task = await _load_task_ref(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_edit(session, user, task)

    if await _upload_next_offset(backend, upload_id, size, chunk_size) != size:
        raise HTTPException(status_code=400, detail="存在未上传分块")

    if backend.name == "local" and hasattr(backend, "root"):
        try:
            du = shutil.disk_usage(backend.root)
            if du.free < size * 1.1:
                raise HTTPException(status_code=400, detail="存储剩余空间不足")
        except (OSError, ValueError):
            pass

    async def _concat():
        for offset in range(0, size, chunk_size):
            async for chunk in backend.stream(_upload_chunk_key(upload_id, offset)):
                yield chunk

    md5 = hashlib.md5()
    sha = hashlib.sha256()
    payload = None
    # P6-6-4 C：加密开启时收集明文 → 加密后落密文；关闭走原明文流（零漂移）
    if get_settings().ARTIFACT_ENCRYPT_ENABLED:
        from app.storage.crypto_gate import crypt_enabled, encrypt_artifact
        from app.storage.governance import record_artifact_meta

        if crypt_enabled():
            payload = b"".join(_concat())
            md5.update(payload)
            sha.update(payload)
            if expect_md5 and md5.hexdigest() != expect_md5:
                raise HTTPException(status_code=400, detail="整体 MD5 不匹配")
            cipher, emeta = await encrypt_artifact(
                payload, task_id=task_id,
                owner_id=getattr(task, "owner_id", None))
            used_size = emeta.get("cipher_size", len(cipher))
            try:
                tag = await backend.put(
                    key, cipher, mode="overwrite",
                    producer_role="user-upload", mime=guess_mime(rel))
            except StorageError as exc:
                raise HTTPException(status_code=500, detail=f"落位失败：{exc}") from exc
            await record_artifact_meta(
                task_id=task_id, rel_path=rel, key=key,
                owner_id=getattr(task, "owner_id", None), size=used_size,
                backend=tag.backend if tag else backend.name,
                sha256=sha.hexdigest(), mime=tag.mime if tag else guess_mime(rel),
                producer_role="user-upload", content_ref=None,
            )
            await _delete_upload(backend, upload_id)
            async with factory() as session:
                await _update_audit(session, task_id, user, "artifact_upload_commit",
                                    {"key": key, "bytes": used_size,
                                     "sha256": sha.hexdigest(),
                                     "encrypted": emeta.get("encrypted")})
            record("upload_commit", backend=backend.name)
            return {"ok": True, "key": key, "size": used_size,
                    "sha256": sha.hexdigest(), "mime": tag.mime if tag else guess_mime(rel),
                    "encrypted": emeta.get("encrypted")}

    async for b in _concat():
        md5.update(b)
        sha.update(b)
    if expect_md5 and md5.hexdigest() != expect_md5:
        raise HTTPException(status_code=400, detail="整体 MD5 不匹配")

    try:
        # P6 配额：写入前预估校验（🔴3；治理关则 no-op）
        from app.storage.governance import (
            QuotaExceededError,
            check_quota,
            dedup_abort,
            dedup_claim,
            dedup_eligible,
            record_artifact_meta,
        )
        try:
            await check_quota(owner_id=getattr(task, "owner_id", None), size=size)
        except QuotaExceededError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

        # O4 去重：占坑优先 → 落内容寻址物理；失败回滚 refs（G1/G2）；碰撞则逐文件
        dedup_content = None
        record_key = key
        dedup_placed = False
        if dedup_eligible(rel_path=rel, size=size, mime=guess_mime(rel)):
            pkey_ph, is_first = await dedup_claim(
                sha256=sha.hexdigest(), size=size, backend=backend)
            if pkey_ph is not None:  # 非碰撞 → 走内容寻址
                try:
                    tag = await backend.put(pkey_ph, _concat(), mode="overwrite",
                                            producer_role="user-upload",
                                            mime=guess_mime(rel))
                except StorageError:
                    await dedup_abort(sha256=sha.hexdigest(), backend=backend)
                    raise
                record_key = pkey_ph
                dedup_content = sha.hexdigest()
                dedup_placed = True
        if not dedup_placed:
            tag = await backend.put(key, _concat(), mode="overwrite",
                                    producer_role="user-upload", mime=guess_mime(rel))
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=f"落位失败：{exc}") from exc

    # P6 权威元表记录 + 配额记账（🔴1/🔴2/🔴3；治理关则 no-op，失败补偿删 key/content）
    await record_artifact_meta(
        task_id=task_id, rel_path=rel, key=record_key,
        owner_id=getattr(task, "owner_id", None), size=size,
        backend=tag.backend if tag else backend.name,
        sha256=sha.hexdigest(), mime=tag.mime if tag else guess_mime(rel),
        producer_role="user-upload", content_ref=dedup_content,
    )

    await _delete_upload(backend, upload_id)
    async with factory() as session:
        await _update_audit(session, task_id, user, "artifact_upload_commit",
                            {"key": key, "bytes": size, "sha256": sha.hexdigest()})
    record("upload_commit", backend=backend.name)
    return {"ok": True, "key": key, "size": size, "sha256": sha.hexdigest(),
            "mime": tag.mime if tag else guess_mime(rel)}


@router.get("/{upload_id}")
async def upload_status(upload_id: str, user: CurrentUser):
    """查询上传进度/断点（⭐）。"""
    _require_upload_enabled()
    backend = get_backend()
    meta = await _read_upload_meta(backend, upload_id)
    task_id = meta["task_id"]
    size = meta["size"]
    chunk_size = meta["chunk_size"]
    task = await _load_task_ref(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_view(session, user, task)
    next_offset = await _upload_next_offset(backend, upload_id, size, chunk_size)
    return {"upload_id": upload_id, "size": size, "received": next_offset,
            "next_offset": next_offset, "done": next_offset >= size,
            "created_at": meta.get("created_at"), "last_active": meta.get("last_active")}


@router.delete("/{upload_id}")
async def upload_cancel(upload_id: str, user: CurrentUser):
    """主动取消并清理暂存（⭐）。"""
    _require_upload_enabled()
    backend = get_backend()
    meta = await _read_upload_meta(backend, upload_id)
    task_id = meta["task_id"]
    task = await _load_task_ref(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    factory = get_session_factory()
    async with factory() as session:
        await _require_edit(session, user, task)
    await _delete_upload(backend, upload_id)
    async with factory() as session:
        await _update_audit(session, task_id, user, "artifact_upload_cancel",
                            {"upload_id": upload_id, "size": meta.get("size")})
    return {"ok": True, "upload_id": upload_id}
