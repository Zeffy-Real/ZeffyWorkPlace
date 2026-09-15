"""P5-1 产物版本管理：StorageBackend 之上的 VersionManager 包装层（审查修订版）。

- **关闭态纯透传**（🔴4）：``ARTIFACT_VERSIONS_ENABLED=false`` 时不做任何额外处理，
  不捕获、不修改返回值，行为与 P5-0 逐字节一致。
- **状态机**（🔴1）：先 DB pending → 存储归档 → 写主 key → DB available+淘汰；
  任一步失败记录置 failed；后台巡检清理 pending/failed 半状态。
- **并发版本号**（🔴2）：(task_id, rel_path) 序列表原子 ``UPDATE ... RETURNING`` 递增。
- **路径安全**（🔴3）：版本 key 统一 ``artifacts/_v/...`` 并经 ``ensure_artifact_key`` 校验。
- 幂等收敛（⭐1）：内容 sha256 + run_id 双重匹配才判定重复，不产生新版本。
- 大文件（⭐5）：> ``ARTIFACT_VERSION_SIZE_LIMIT`` 不自动归档。
- 级联（🔴5）：删除主 key 同步删版本存储 + 表记录；单版本删除后版本号不重排。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Sequence
from typing import Any

from app.config import get_settings
from app.db import repos
from app.storage.base import (
    ArtifactMeta,
    StorageBackend,
    StorageError,
    ensure_artifact_key,
    guess_mime,
)

logger = logging.getLogger(__name__)

V_ARCHIVE = "available"
V_PENDING = "pending"
V_FAILED = "failed"


def version_key(task_id: str, rel_path: str, version: int) -> str:
    """版本归档 key：``artifacts/_v/{task_id}/{rel_sha1}/v{n}``（🔴3 统一校验）。"""
    rel_sha = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]  # noqa: S324 仅定长散列，非安全用途
    key = f"artifacts/_v/{task_id}/{rel_sha}/v{version}"
    ensure_artifact_key(key)
    return key


def _split(key: str) -> tuple[str, str]:
    ensure_artifact_key(key)
    rel = key[len("artifacts/"):]
    task_id, rel_path = rel.split("/", 1)
    return task_id, rel_path


class VersionManager(StorageBackend):
    """版本化后端包装：开启时接管 put/delete 产生版本链；关闭时纯透传。"""

    name = "versioned"

    def __init__(self, backend: StorageBackend, *, session_factory: Any = None) -> None:
        self._b = backend
        self._sf = session_factory
        self._s = get_settings()

    # ---- 配置 ----

    @property
    def enabled(self) -> bool:
        return self._s.ARTIFACT_VERSIONS_ENABLED

    def _session_factory(self):
        if self._sf is not None:
            return self._sf
        from app.db.base import get_session_factory

        return get_session_factory()

    # ---- 透传（关闭态/未涉及版本的操作恒直通） ----

    async def get(self, key: str) -> bytes | None:
        return await self._b.get(key)

    async def stream(self, key: str, start: int = 0):
        async for chunk in self._b.stream(key, start=start):
            yield chunk

    async def size(self, key: str) -> int | None:
        return await self._b.size(key)

    async def fingerprint(self, key: str) -> str | None:
        return await self._b.fingerprint(key)

    async def exists(self, key: str) -> bool:
        return await self._b.exists(key)

    async def list(self, prefix: str) -> list[str]:
        return await self._b.list(prefix)

    async def health(self) -> dict:
        return await self._b.health()

    async def close(self) -> None:
        await self._b.close()

    # ---- 写入：版本流程 ----

    async def put(self, key: str, data, mode: str = "overwrite", *, run_id: str = "",
                  producer_role: str = "", mime: str | None = None,
                  preserve_abs: bool = True) -> ArtifactMeta:
        if not self.enabled or mode != "overwrite":
            # 🔴4 纯透传：不捕获、不修改返回值
            return await self._b.put(key, data, mode=mode, run_id=run_id,
                                     producer_role=producer_role, mime=mime,
                                     preserve_abs=preserve_abs)
        return await self._put_versioned(key, data, run_id=run_id,
                                         producer_role=producer_role, mime=mime)

    async def _put_versioned(self, key: str, data, *, run_id: str,
                             producer_role: str, mime: str | None) -> ArtifactMeta:
        task_id, rel_path = _split(key)
        payload = bytes(data) if isinstance(data, (bytes, bytearray)) else None
        if payload is None:
            # 流式数据不归档（无法预取校验和），走透传（不产生版本）
            return await self._b.put(key, data, mode="overwrite", run_id=run_id,
                                     producer_role=producer_role, mime=mime)
        sha = hashlib.sha256(payload).hexdigest()
        sm = self._s
        sf = self._session_factory()

        # ⭐1 幂等收敛：同内容 + 同 run_id 且最新 available 已记录 → 不产生新版本
        async with sf() as s:
            latest = await repos.latest_available_version(s, task_id=task_id, rel_path=rel_path)
            if latest and latest.run_id == run_id and latest.sha256 == sha:
                return ArtifactMeta(
                    key=key, task_id=task_id, rel_path=rel_path, size=len(payload),
                    mode="overwrite", exists=True, mime=mime or guess_mime(rel_path),
                    run_id=run_id, producer_role=producer_role,
                    md5=hashlib.md5(payload).hexdigest(), sha256=sha,
                    abs_path=None, url=None, backend=self._b.name,
                )

        archive = True
        if len(payload) > sm.ARTIFACT_VERSION_SIZE_LIMIT:
            archive = False  # ⭐5 大文件不自动归档

        # 1) 版本号 + pending 记录
        version = await self._alloc_version(sf, task_id, rel_path)
        akey = version_key(task_id, rel_path, version) if archive else ""
        record = await self._create_pending(sf, task_id, rel_path, version, akey,
                                            producer_role, run_id)
        rec_id = record.id

        try:
            # 2) 写主 key（原原子逻辑）
            await self._b.put(key, payload, mode="overwrite", run_id=run_id,
                              producer_role=producer_role, mime=mime)
            # 3) 写后归档：新内容复制到 akey(version)（v1..vN 均保留对象，读/比对一致）
            if archive:
                await self._b.put(akey, payload, mode="overwrite")
        except Exception as exc:  # noqa: BLE001 任何一步失败 → failed，不污染版本链
            with contextlib.suppress(Exception):  # noqa: BLE001
                await self._mark(sf, rec_id, V_FAILED)
            logger.warning("版本写入失败 task=%s rel=%s v%s：%s", task_id, rel_path, version, exc)
            raise StorageError(f"版本写入失败：{exc}") from exc

        # 4) available + 淘汰
        await self._mark(sf, rec_id, V_ARCHIVE, size=len(payload), sha256=sha,
                         mime=mime or guess_mime(rel_path))
        # P6 版本→元表同步（治理开启）：归档版本纳入元表 + 配额（🔴1 防无限版本绕配额）
        if self._s.ARTIFACT_META_ENABLED:
            sr_owner = await self._owner_for_task(sf, task_id)
            await self._record_version_meta(task_id, rel_path, akey, sr_owner,
                                            len(payload), sha, mime or guess_mime(rel_path), version)
        pruned = await self._prune(sf, task_id, rel_path, sm.ARTIFACT_MAX_VERSIONS)
        for pk in pruned:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await self._b.delete(pk)
            if pk and self._s.ARTIFACT_META_ENABLED:
                await self._release_version_meta(pk)

        return ArtifactMeta(
            key=key, task_id=task_id, rel_path=rel_path, size=len(payload),
            mode="overwrite", exists=True, mime=mime or guess_mime(rel_path),
            run_id=run_id, producer_role=producer_role,
            md5=hashlib.md5(payload).hexdigest(), sha256=sha,
            abs_path=None, url=None, backend=self._b.name,
        )

    # ---- 删除：级联版本（🔴5） ----

    async def delete(self, key: str) -> bool:
        if not self.enabled:
            return await self._b.delete(key)
        task_id, rel_path = _split(key)
        ok = await self._b.delete(key)
        # 级联删版本存储 + 表记录
        sf = self._session_factory()
        async with sf() as s:
            rows = await repos.all_version_records(s, task_id=task_id, rel_path=rel_path)
            keys = [r.key for r in rows if r.key]
        for vk in keys:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await self._b.delete(vk)
            if vk and self._s.ARTIFACT_META_ENABLED:
                await self._release_version_meta(vk)
        async with sf() as s:
            await repos.delete_version_records_by_rel(s, task_id=task_id, rel_path=rel_path)
        return ok

    # ---- 版本专属 API（/artifacts 调用） ----

    async def list_versions(self, task_id: str, rel_path: str, *,
                            page: int = 1, page_size: int = 50) -> dict:
        sf = self._session_factory()
        async with sf() as s:
            items, total, total_bytes = await repos.list_versions(
                s, task_id=task_id, rel_path=rel_path, page=page, page_size=page_size)
            return {
                "items": [self._row(r) for r in items],
                "total": total, "total_bytes": total_bytes, "page": page,
            }

    async def get_version_bytes(self, task_id: str, rel_path: str, version: int) -> bytes | None:
        sf = self._session_factory()
        async with sf() as s:
            rec = await repos.get_version(s, task_id=task_id, rel_path=rel_path, version=version)
            if rec is None or not rec.key:
                return None
            akey = rec.key
        return await self._b.get(akey)

    async def get_version_meta(self, task_id: str, rel_path: str, version: int) -> dict | None:
        sf = self._session_factory()
        async with sf() as s:
            rec = await repos.get_version(s, task_id=task_id, rel_path=rel_path, version=version)
            return self._row(rec) if rec else None

    async def delete_version(self, task_id: str, rel_path: str, version: int) -> bool:
        sf = self._session_factory()
        async with sf() as s:
            rec = await repos.get_version(s, task_id=task_id, rel_path=rel_path, version=version)
            if rec is None:
                return False
            akey = rec.key
            rid = rec.id
        if akey:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await self._b.delete(akey)
            if akey and self._s.ARTIFACT_META_ENABLED:
                await self._release_version_meta(akey)
        async with sf() as s:
            return await repos.delete_version_record(s, record_id=rid)

    async def diff_versions(self, task_id: str, rel_path: str, from_v: int, to_v: int) -> dict:
        """行级 diff（🔴6 资源约束：DB size 阈值 + 预览截断 + 二进制 not_text）。"""
        sm = self._s
        sf = self._session_factory()
        async with sf() as s:
            a = await repos.get_version(s, task_id=task_id, rel_path=rel_path, version=from_v)
            b = await repos.get_version(s, task_id=task_id, rel_path=rel_path, version=to_v)
            if a is None or b is None:
                return {"status": "missing", "detail": "版本不存在"}
            if a.size > sm.ARTIFACT_DIFF_MAX_SIZE or b.size > sm.ARTIFACT_DIFF_MAX_SIZE:
                return {"status": "too_large", "detail": f"版本超过 diff 大小上限 {sm.ARTIFACT_DIFF_MAX_SIZE} 字节"}
            mime = a.mime or b.mime or guess_mime(rel_path)
            if not mime.startswith("text/") and mime not in {"application/json", "application/sql",
                                                              "text/markdown", "text/x-python", "application/x-yaml"}:
                return {"status": "not_text", "detail": f"二进制类型不支持行级 diff：{mime}"}
            ka, kb = a.key, b.key

        text_a = await self._read_text(ka)
        text_b = await self._read_text(kb)
        if text_a is None or text_b is None:
            return {"status": "missing", "detail": "版本文件缺失"}

        import difflib

        lines_a = text_a.splitlines()
        lines_b = text_b.splitlines()
        diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=f"v{from_v}", tofile=f"v{to_v}",
                                         lineterm=""))
        added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        truncated = len(diff) > sm.ARTIFACT_DIFF_PREVIEW_LINES
        preview = diff[: sm.ARTIFACT_DIFF_PREVIEW_LINES]
        return {
            "status": "ok", "from": from_v, "to": to_v,
            "changed": added + removed, "added": added, "removed": removed,
            "truncated": truncated, "preview": preview,
        }

    # ---- 内部 ----

    async def _owner_for_task(self, sf, task_id: str):
        try:
            async with sf() as s:
                return await repos.get_owner_or_none(s, task_id)
        except Exception:  # noqa: BLE001
            return None

    async def _record_version_meta(self, task_id, rel_path, akey, owner, size, sha, mime, version):
        from app.storage.governance import record_version_meta

        try:
            await record_version_meta(
                task_id=task_id, rel_path=rel_path, archive_key=akey,
                owner_id=owner, size=size, sha256=sha, mime=mime, version=version)
        except Exception as exc:  # noqa: BLE001
            logger.warning("版本→元表同步失败 v%s key=%s: %s", version, akey, exc)

    async def _release_version_meta(self, akey: str):
        from app.storage.governance import release_version_meta

        try:
            await release_version_meta(archive_key=akey)
        except Exception as exc:  # noqa: BLE001
            logger.warning("版本元表释放失败 key=%s: %s", akey, exc)

    async def _read_text(self, key: str) -> str | None:
        data = await self._b.get(key)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    async def _alloc_version(self, sf, task_id: str, rel_path: str) -> int:
        # 唯一约束/序列冲突 → 重试（并发兜底）
        for _ in range(3):
            try:
                async with sf() as s:
                    return await repos.next_version(s, task_id=task_id, rel_path=rel_path)
            except repos.RepositoryError:
                continue
        raise StorageError("分配版本号失败（并发冲突）")

    async def _create_pending(self, sf, task_id, rel_path, version, akey,
                              producer_role, run_id) -> Any:
        async with sf() as s:
            return await repos.create_version_record(
                s, task_id=task_id, rel_path=rel_path, version=version, key=akey,
                producer_role=producer_role, run_id=run_id, mode="overwrite")

    async def _mark(self, sf, rec_id, status, *, size=0, sha256="", mime="") -> None:
        async with sf() as s:
            await repos.update_version_status(s, record_id=rec_id, status=status,
                                             size=size, sha256=sha256, mime=mime)

    async def _prune(self, sf, task_id, rel_path, keep_max) -> Sequence[str]:
        async with sf() as s:
            return await repos.prune_versions(s, task_id=task_id, rel_path=rel_path,
                                              keep_max=max(1, keep_max))

    @staticmethod
    def _row(r) -> dict:
        return {
            "version": r.version, "key": r.key, "status": r.status, "size": r.size,
            "sha256": r.sha256, "mime": r.mime, "producer_role": r.producer_role,
            "run_id": r.run_id, "mode": r.mode, "created_at": r.created_at.isoformat()
            if r.created_at else None,
        }
