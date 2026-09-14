"""P5 分布式产物存储：后端抽象 + 统一 key 空间 + 契约（究极审查修订版）。

设计要点（对齐 .trae/documents/P5-0 修订）：
- **统一后端接口契约**（🔴1）：``put(key, data, mode='overwrite')`` 强制支持
  ``overwrite / no_overwrite / new`` 三种幂等模式，跨后端（Local/S3）行为完全一致，
  Agent 层无感知切换。
- **key 路径逃逸防护**（🔴2）：所有操作必须经 ``normalize_artifact_key`` 校验，
  最终 key 严格限定 ``artifacts/{task_id}/{rel_path}``，越界抛 ``SecurityError``。
- 实现方（LocalBackend/S3Backend）各自保证原子写兜底（🔴1）：S3 用临时 key + copy + 删除；
  本地用临时文件 + rename。
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


class StorageError(Exception):
    """存储后端异常（IO 失败、后端不可用等）。"""


class SecurityError(StorageError):
    """key 越权/越界访问（路径逃逸、非法 task_id/rel_path）。"""


class FileExistsError_(StorageError):
    """目标已存在且 mode=no_overwrite，拒绝覆盖（幂等保护）。"""


class IntegrityError(StorageError):
    """读取内容校验和（MD5/SHA256）不匹配，产物损坏。"""


# ---------------------------------------------------------------------------
# key 空间与规范化（🔴2：防路径逃逸的唯一闸口）
# ---------------------------------------------------------------------------

_ARTIFACT_PREFIX = "artifacts/"
_TMP_PREFIX = "artifacts/_tmp/"

# 允许的 rel_path 字符：字母/数字/._- 与 /（目录分隔）；禁止空段、`.`/`..`、控制字符。
_RE_BAD_PATH = re.compile(r"(^\.\.$|^\.$|\.\./|/\.\.|[\x00-\x1f\x7f])")


def normalize_artifact_key(task_id: str, rel_path: str, *, allow_tmp: bool = False) -> str:
    """把 ``(task_id, rel_path)`` 规范化为受限 key；越界/非法一律抛 SecurityError。

    - ``task_id``：非空、不包含 ``/`` 与 ``..`` 段，长度 ≤ 64；
    - ``rel_path``：非空、相对路径，禁止绝对路径、空、``..`` 跳转、控制字符；
    - 返回值严格满足 ``artifacts/{task_id}/{rel_path}``。
    :param allow_tmp: True 时允许 rel_path 位于 ``_tmp/``（S3 原子写临时 key）。
    """
    if not task_id or not isinstance(task_id, str):
        raise SecurityError(f"非法 task_id：{task_id!r}")
    if "/" in task_id or "\\" in task_id or task_id in (".", ".."):
        raise SecurityError(f"非法 task_id（不得含路径分隔/跳转）：{task_id!r}")
    if len(task_id) > 64:
        raise SecurityError(f"task_id 过长：{task_id[:32]}...")

    if not rel_path or not isinstance(rel_path, str):
        raise SecurityError("rel_path 不能为空")
    # Windows 风格分隔符统一转 `/` 再校验
    rel = rel_path.replace("\\", "/")
    if rel.startswith("/"):
        raise SecurityError(f"禁止绝对路径：{rel_path!r}")
    if rel.endswith("/"):
        rel = rel.rstrip("/")
        if not rel:
            raise SecurityError(f"非法 rel_path：{rel_path!r}")
    if _RE_BAD_PATH.search(rel):
        raise SecurityError(f"非法 rel_path（含 .. / 控制字符）：{rel_path!r}")
    # 目录段本身不得为 `.`/`..`（pathlib 会在之后 resolve，这里先拦截）
    for seg in rel.split("/"):
        if seg in ("", ".", ".."):
            raise SecurityError(f"非法 rel_path 段：{rel_path!r}")
    if not allow_tmp and rel.startswith("_tmp/"):
        raise SecurityError("禁止直接访问临时 key 空间：_tmp/")

    return f"{_ARTIFACT_PREFIX}{task_id}/{rel}"


def tmp_key() -> str:
    """生成 S3 原子写临时 key：``artifacts/_tmp/{uuid}.part``（🔴5 统一命名）。"""
    return f"{_TMP_PREFIX}{uuid.uuid4().hex}.part"


def ensure_artifact_key(key: str) -> None:
    """校验既有 key 仍在 ``artifacts/`` 前缀内（读/列/删前统一闸口）。"""
    if not key.startswith(_ARTIFACT_PREFIX):
        raise SecurityError(f"key 越界（须在 {_ARTIFACT_PREFIX!r} 前缀内）：{key!r}")
    # 剥离前缀后再跑一次 rel 校验（防 `..` 藏在前缀后）
    rel = key[len(_ARTIFACT_PREFIX):]
    if "/" not in rel or rel.startswith("/"):
        raise SecurityError(f"非法 key：{key!r}")
    task_id, rel_path = rel.split("/", 1)
    normalize_artifact_key(task_id, rel_path, allow_tmp=rel.startswith("_tmp/"))


# ---------------------------------------------------------------------------
# 元数据（⭐1：产物元数据标准化）
# ---------------------------------------------------------------------------


@dataclass
class ArtifactMeta:
    """一次 put 返回的产物元数据（写入方记录到 TaskNode.output / AuditLog）。"""

    key: str
    task_id: str
    rel_path: str
    size: int
    mode: str
    exists: bool
    mime: str = "application/octet-stream"
    run_id: str = ""
    producer_role: str = ""
    md5: str = ""
    sha256: str = ""
    abs_path: str | None = None  # 🔴3 兼容：Local 返回真实路径；S3 为 None
    url: str | None = None  # 可访问地址（本地直链可选 / S3 预签名）
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    backend: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "task_id": self.task_id,
            "rel_path": self.rel_path,
            "size": self.size,
            "mode": self.mode,
            "exists": self.exists,
            "mime": self.mime,
            "run_id": self.run_id,
            "producer_role": self.producer_role,
            "md5": self.md5,
            "sha256": self.sha256,
            "abs_path": self.abs_path,
            "url": self.url,
            "created_at": self.created_at,
            "backend": self.backend,
        }


def guess_mime(rel_path: str) -> str:
    ext = os.path.splitext(rel_path)[1].lower()
    return {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
        ".csv": "text/csv",
        ".html": "text/html",
        ".js": "text/javascript",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".zip": "application/zip",
        ".sql": "application/sql",
        ".py": "text/x-python",
    }.get(ext, "application/octet-stream")


def compute_checksums(data: bytes) -> tuple[str, str]:
    import hashlib

    return hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 后端抽象（🔴1：统一契约）
# ---------------------------------------------------------------------------


class StorageBackend:
    """产物存储后端接口。所有实现必须完整对齐三种写入模式与流式读写。"""

    name = "base"

    async def put(self, key: str, data: bytes | object, mode: str = "overwrite",
                  *, run_id: str = "", producer_role: str = "",
                  mime: str | None = None, preserve_abs: bool = True) -> ArtifactMeta:
        """写入产物。

        :param data: bytes 或可迭代 bytes（流式写入，⭐2）。
        :param mode: overwrite | no_overwrite | new。
        :raises FileExistsError_: no_overwrite 且已存在。
        """
        raise NotImplementedError

    async def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        """流式读取（分块 async iterator）；文件不存在抛 StorageError。"""
        raise NotImplementedError
        yield b""  # pragma: no cover 类型标记（async generator 声明）

    async def exists(self, key: str) -> bool:
        raise NotImplementedError

    async def delete(self, key: str) -> bool:
        raise NotImplementedError

    async def list(self, prefix: str) -> list[str]:
        """列出前缀下全部 key（含相对 rel_path 后缀）。"""
        raise NotImplementedError

    async def health(self) -> dict[str, Any]:
        """存储健康（并入 /health；S3 挂返回 ok=False → degraded，🔴5）。"""
        return {"ok": True, "backend": self.name, "detail": "ok"}

    async def close(self) -> None:
        """释放连接资源（进程退出/切换后端时调用）。"""
        return None
