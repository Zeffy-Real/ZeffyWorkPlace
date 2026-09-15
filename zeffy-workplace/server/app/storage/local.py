"""P5 本地磁盘后端（默认，兼容 P4 零漂移）。

- key → ``<STORAGE_ROOT>/<key>``（默认 STORAGE_ROOT=WORKSPACE_ROOT → 行为与 P4 完全一致）。
- **存量兼容映射**（🔴3）：P4 旧路径 ``task-<id>/<path>`` 读时映射到原 WORKSPACE_ROOT 路径，
  保证升级后存量产物可访问；并提供迁移脚本（app/storage/tools/migrate_local.py）。
- 原子写：「临时文件 + rename」；``abs_path`` 保留真实路径（兼容依赖本地路径的逻辑）。
- 临时文件统一命名 ``<STORAGE_ROOT>/_tmp/*.tmp``（🔴5，GC 清理）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path

from app.storage.base import (
    ArtifactMeta,
    FileExistsError_,
    RangeNotSatisfiableError,
    SecurityError,
    StorageBackend,
    StorageError,
    ensure_artifact_key,
    guess_mime,
    normalize_artifact_key,
    validate_start,
)

logger = logging.getLogger(__name__)

_CHUNK = 1 << 16  # 64 KiB 流式分块


class LocalBackend(StorageBackend):
    name = "local"

    def __init__(self, root: str | Path, *, legacy_root: str | Path | None = None) -> None:
        """本地后端。

        :param root: 新 key 空间根（key ``artifacts/...`` 落于此，默认 WORKSPACE_ROOT）。
        :param legacy_root: 存量根（P4 旧 ``task-<id>/`` 所在；默认 root 本身，
            因 P4 文件即位于 ``<WORKSPACE_ROOT>/task-<id>/``）。
        """
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.legacy_root = Path(legacy_root).resolve() if legacy_root is not None else self.root
        self._lock = asyncio.Lock()

    # ---- 内部路径解析 ----

    def _key_to_path(self, key: str) -> Path:
        ensure_artifact_key(key)
        rel = key[len("artifacts/"):]  # 含 task_id
        return (self.root / "artifacts" / rel).resolve()

    def _cold_path(self, key: str) -> Path:
        ensure_artifact_key(key)
        rel = key[len("artifacts/"):]
        return (self.root / "_cold" / rel).resolve()

    def _resolve_write(self, key: str) -> Path:
        """写入定位：hot 优先（新产物总是 hot）。"""
        return self._key_to_path(key)

    def _split_key(self, key: str) -> tuple[str, str]:
        ensure_artifact_key(key)
        rel = key[len("artifacts/"):]
        task_id, rel_path = rel.split("/", 1)
        return task_id, rel_path

    def _resolve_read(self, key: str) -> Path | None:
        """读取时：新 key hot → cold 归档 → 存量映射（🔴3 + P6 分层透明路由）。"""
        new = self._key_to_path(key)
        if new.is_file():
            return new
        cold = self._cold_path(key)
        if cold.is_file():
            return cold
        task_id, rel_path = self._split_key(key)
        return self._legacy_path(task_id, rel_path)

    def _legacy_path(self, task_id: str, rel_path: str) -> Path | None:
        """存量映射：旧路径 ``legacy_root/task-<id>/<rel>``（无则 None）。"""
        base = (self.legacy_root / f"task-{task_id}").resolve()
        try:
            target = (base / rel_path).resolve()
        except (OSError, ValueError):
            return None
        if target != base and base not in target.parents:
            return None
        return target if target.is_file() else None

    # ---- 统一契约实现 ----

    async def put(self, key: str, data, mode: str = "overwrite", *, run_id: str = "",
                  producer_role: str = "", mime: str | None = None,
                  preserve_abs: bool = True) -> ArtifactMeta:
        if mode not in {"overwrite", "no_overwrite", "new"}:
            raise StorageError(f"非法写入模式：{mode!r}")
        task_id, rel_path = self._split_key(key)
        if mode == "new":
            rel_path = self._new_rel_path(rel_path, run_id)
            key = normalize_artifact_key(task_id, rel_path)

        target = self._key_to_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        if mode == "no_overwrite" and target.exists():
            raise FileExistsError_(f"文件已存在且 mode=no_overwrite，拒绝覆盖：{rel_path}")

        tmp_root = self.root / "_tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        tmp = tmp_root / f"{uuid.uuid4().hex}.tmp"
        try:
            if isinstance(data, (bytes, bytearray)):
                tmp.write_bytes(bytes(data))
            else:  # 流式（AsyncIterable / iterable）
                with tmp.open("wb") as fh:
                    async for chunk in data:
                        fh.write(chunk)
            os.replace(tmp, target)
        except BaseException:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # noqa: BLE001
                    pass
            raise

        md5, sha256 = self._checksums_from_file(target)
        return ArtifactMeta(
            key=key, task_id=task_id, rel_path=rel_path,
            size=target.stat().st_size, mode=mode, exists=True,
            mime=mime or guess_mime(rel_path), run_id=run_id,
            producer_role=producer_role, md5=md5, sha256=sha256,
            abs_path=str(target) if preserve_abs else None,
            url=None, backend=self.name,
        )

    async def get(self, key: str) -> bytes | None:
        target = self._resolve_read(key)
        if target is None:
            return None
        try:
            return target.read_bytes()
        except OSError as exc:
            raise StorageError(f"读取产物失败：{target} ({exc})") from exc

    async def stream(self, key: str, start: int = 0):
        target = self._resolve_read(key)
        if target is None:
            raise StorageError(f"产物不存在：{key}")
        # 🔴1 独立句柄：每次调用独立打开 + with 自动关闭（禁止共享句柄）
        # 🔴3 边界输入：start 须为非负整数且 < size；负数/浮点/bool/越界统一 416
        size = target.stat().st_size
        validate_start(start, size)
        with target.open("rb") as fh:
            try:
                if start > 0:
                    fh.seek(start)
            except (OSError, ValueError) as exc:
                raise RangeNotSatisfiableError(f"Range 偏移越界：{start}") from exc
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                yield chunk

    async def size(self, key: str) -> int | None:
        target = self._resolve_read(key)
        if target is None:
            return None
        try:
            return target.stat().st_size
        except OSError:
            return None

    async def fingerprint(self, key: str) -> str | None:
        """🔴2 Local 强指纹：size + mtime_ns，重写（含同大小）即变更。"""
        target = self._resolve_read(key)
        if target is None:
            return None
        try:
            st = target.stat()
            return f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            return None

    async def exists(self, key: str) -> bool:
        return self._resolve_read(key) is not None

    async def delete(self, key: str) -> bool:
        # P6 分层：hot 与 cold 均可能；统一经 _resolve_read 定位实际文件
        target = self._resolve_read(key)
        if target is None:
            return False
        try:
            target.unlink()
            self._prune_empty_dirs(target.parent)
            return True
        except OSError as exc:
            raise StorageError(f"删除产物失败：{key} ({exc})") from exc

    async def list(self, prefix: str) -> list[str]:
        """列出前缀下全部产物 key（hot + cold 归档 + 存量兼容，🔴3）。

        - cold 产物以原 key（artifacts/...) 列出，仅当 hot 空间无同名时（P6 透明路由）。
        """
        self._check_prefix(prefix)
        keys: list[str] = []
        base = (self.root / prefix.replace("/", os.sep)).resolve()
        if base.exists():
            for p in sorted(base.rglob("*")):
                if not p.is_file() or "_tmp" in p.parts or "_cold" in p.parts:
                    continue
                rel = p.relative_to(self.root).as_posix()
                keys.append(f"artifacts/{rel.split('artifacts/', 1)[1]}")
        # P6 cold 归档：遍历 _cold/<prefix>，hot 不存在则映射回原 key
        cold_prefix = prefix[len("artifacts/"):] if prefix.startswith("artifacts/") else prefix
        cold_base = (self.root / "_cold" / cold_prefix.replace("/", os.sep)).resolve()
        if cold_base.exists():
            for p in sorted(cold_base.rglob("*")):
                if not p.is_file():
                    continue
                crel = p.relative_to(self.root / "_cold").as_posix()
                key = f"artifacts/{crel}"
                if not self._key_to_path(key).exists():
                    keys.append(key)
        # 存量兼容：prefix 形如 artifacts/{task_id}
        if prefix.startswith("artifacts/") and prefix != "artifacts/":
            task_id = prefix[len("artifacts/"):].split("/", 1)[0]
            legacy = (self.legacy_root / f"task-{task_id}").resolve()
            if legacy.exists():
                for p in sorted(legacy.rglob("*")):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(legacy).as_posix()
                    key = f"artifacts/{task_id}/{rel}"
                    if self._key_to_path(key).exists():
                        continue  # 新空间已存在则不去重展示（避免双份）
                    keys.append(key)
        return sorted(set(keys))

    async def archive_cold(self, key: str) -> bool:
        """P6 分层：把 hot 物理文件移动到 _cold/ 归档（key 不变，读路由透明）。"""
        hot = self._key_to_path(key)
        if not hot.is_file():
            # 已在 cold → 视为成功（幂等）
            return True
        cold = self._cold_path(key)
        try:
            cold.parent.mkdir(parents=True, exist_ok=True)
            os.replace(hot, cold)
            self._prune_empty_dirs(hot.parent)
            return True
        except OSError as exc:
            raise StorageError(f"归档冷存储失败：{key} ({exc})") from exc

    # ---- 辅助 ----

    @staticmethod
    def _check_prefix(prefix: str) -> None:
        if not prefix.startswith("artifacts/"):
            raise SecurityError(f"list 前缀越界（须在 artifacts/ 内）：{prefix!r}")
        if any(seg in ("..", ".") for seg in prefix.replace("\\", "/").split("/")):
            raise SecurityError(f"list 前缀非法：{prefix!r}")

    def _new_rel_path(self, rel_path: str, run_id: str) -> str:
        stem, ext = os.path.splitext(rel_path)
        return f"{stem}-{run_id}{ext}" if run_id else f"{stem}-{_timestamp()}{ext}"

    @staticmethod
    def _checksums_from_file(path: Path) -> tuple[str, str]:
        md5, sha256 = hashlib.md5(), hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                md5.update(chunk)
                sha256.update(chunk)
        return md5.hexdigest(), sha256.hexdigest()

    def _prune_empty_dirs(self, start: Path) -> None:
        cur = start
        try:
            while cur != self.root and cur.exists() and not any(cur.iterdir()):
                cur.rmdir()
                cur = cur.parent
        except OSError:  # noqa: BLE001
            pass

    async def health(self) -> dict:
        try:
            probe = self.root / "_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return {"ok": True, "backend": self.name, "detail": "writable"}
        except OSError as exc:
            return {"ok": False, "backend": self.name, "detail": f"unwritable: {exc}"}


def _timestamp() -> str:
    import time

    return time.strftime("%Y%m%d%H%M%S")
