"""文件系统工具（P1-3，P5 改造：IO 目标换到 StorageBackend key 空间）。

安全约束（🔴，审查强制）：
- 所有文件读写**必须**经 ``normalize_artifact_key`` 解析（统一 key 闸口），
  越界/非法路径抛 ``SecurityError``（本地模式下等价于旧 ``safe_resolve_workspace_path`` 防逃逸）。
- 幂等防护（🔴1）：三种写入模式 ``overwrite / no_overwrite / new`` 由后端统一契约保证，
  跨 Local/S3 后端行为完全一致；原子写由后端兜底（临时文件+rename / 临时 key+copy）。
- 协议**向下兼容**（🔴3）：返回字段在旧 ``{path, abs_path, mode, bytes, exists}`` 基础上
  **新增** ``key/url``；本地后端保留 ``abs_path`` 真实路径，S3 后端为 None。
- 失败降级（🔴5）：S3 运行时故障自动降级本地重试一次，仍失败才报错并记审计。

一切操作失败抛 ``ToolPermissionError`` / ``ToolError``，由注册表转为失败结果。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.storage import fallback_local, record
from app.storage.base import (
    FileExistsError_,
    SecurityError,
    StorageBackend,
    StorageError,
    normalize_artifact_key,
)
from app.tools.registry import ToolError, ToolPermissionError, ToolSpec

logger = logging.getLogger(__name__)


def safe_resolve_workspace_path(workspace_root: str | Path, rel: str) -> Path:
    """把 ``rel`` 解析为工作区根下的绝对路径；逃逸根目录则抛 ToolPermissionError。

    P5 保留此函数供外部调用方/存量逻辑使用；fs 工具内部已改走 ``normalize_artifact_key``。
    """
    root = Path(workspace_root).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ToolPermissionError(f"路径越界，禁止访问工作区之外：{rel!r}")
    return target


def _default_backend(workspace_root: str | Path, backend: StorageBackend | None) -> StorageBackend:
    """工具默认后端：显式注入优先；配了 s3 走全局单例；否则按 workspace_root 建本地（测试隔离）。"""
    if backend is not None:
        return backend
    from app.config import get_settings

    s = get_settings()
    if s.STORAGE_BACKEND == "s3" and s.S3_ENDPOINT:
        from app.storage import get_backend

        return get_backend()
    from app.storage.local import LocalBackend

    return LocalBackend(workspace_root)


def _key_for(task_id: str, path: str, mode: str = "overwrite") -> str:
    """fs 工具统一 key 闸口：非法路径 → ToolPermissionError（越权语义）。"""
    try:
        return normalize_artifact_key(task_id, path)
    except SecurityError as exc:
        raise ToolPermissionError(str(exc)) from exc


async def _write_file(*, backend: StorageBackend, task_id: str, path: str,
                      content: str, mode: str = "no_overwrite",
                      run_id: str = "") -> dict:
    """写文件工具处理函数。

    :param mode: overwrite | no_overwrite | new
    """
    if mode not in {"overwrite", "no_overwrite", "new"}:
        raise ToolError(f"非法写入模式：{mode!r}（可选 overwrite/no_overwrite/new）")

    key = _key_for(task_id, path, mode)
    data = content.encode("utf-8")
    try:
        meta = await backend.put(key, data, mode=mode, run_id=run_id, producer_role="agent")
    except FileExistsError_ as exc:
        raise ToolError(str(exc)) from exc
    except StorageError as exc:
        # 🔴5 S3 运行时故障：降级本地重试一次；仍失败才报错
        if backend.name == "s3":
            logger.warning("S3 写入失败，降级本地重试一次：%s", exc)
            record("put", backend=backend.name, nbytes=len(data), error=True)
            try:
                meta = await fallback_local().put(key, data, mode=mode, run_id=run_id,
                                                  producer_role="agent")
            except StorageError as exc2:
                raise ToolError(f"存储写入失败（含本地兜底）：{exc2}") from exc2
        else:
            raise ToolError(f"存储写入失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"存储写入异常：{type(exc).__name__}: {exc}") from exc

    record("put", backend=meta.backend, nbytes=meta.size)
    return {
        "path": meta.rel_path,
        "abs_path": meta.abs_path,   # 🔴3 兼容：本地真实路径；S3=None
        "mode": meta.mode,
        "bytes": meta.size,
        "exists": meta.exists,
        "key": meta.key,
        "url": meta.url,
    }


async def _read_file(*, backend: StorageBackend, task_id: str, path: str) -> dict:
    key = _key_for(task_id, path)
    data = await _get_with_fallback(backend, key)
    if data is None:
        raise ToolError(f"文件不存在：{path}")
    return {"path": path, "content": data.decode("utf-8", errors="replace")}


async def _list_dir(*, backend: StorageBackend, task_id: str, path: str = "") -> dict:
    if path and path.strip():
        prefix = _key_for(task_id, path)
    else:
        # 空 path = 任务根目录（兼容旧 fs_list(path="") 行为）
        try:
            normalize_artifact_key(task_id, "x")
        except SecurityError as exc:
            raise ToolPermissionError(str(exc)) from exc
        prefix = f"artifacts/{task_id}"
    try:
        keys = await backend.list(prefix)
    except StorageError as exc:
        raise ToolError(f"列出目录失败：{exc}") from exc
    record("list", backend=backend.name)
    # 只取相对该前缀的直下一层条目名（兼容旧 fs_list 语义）
    base = f"{prefix}/"
    entries: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if not key.startswith(base):
            continue
        rest = key[len(base):]
        name = rest.split("/", 1)[0]
        if name and name not in seen:
            seen.add(name)
            entries.append(name)
    return {"path": path or ".", "entries": sorted(entries), "count": len(entries)}


async def _get_with_fallback(backend: StorageBackend, key: str) -> bytes | None:
    """读取：S3 故障时降级本地兜底；否则直接取。"""
    try:
        data = await backend.get(key)
        record("get", backend=backend.name, nbytes=len(data) if data else 0)
        return data
    except StorageError as exc:
        if backend.name != "s3":
            raise
        logger.warning("S3 读取失败，降级本地：%s", exc)
        record("get", backend=backend.name, error=True)
        try:
            data = await fallback_local().get(key)
            record("get", backend="local", nbytes=len(data) if data else 0)
            return data
        except StorageError:
            return None


def make_fs_tools(workspace_root: str | Path,
                  backend: StorageBackend | None = None) -> list[ToolSpec]:
    """构建 fs 相关工具规范列表（默认权限等级 'fs'）。

    :param backend: 显式存储后端（测试注入）；None 则按配置构建（local 默认 / s3 可选）。
    """
    root = str(Path(workspace_root).resolve())
    _backend = _default_backend(root, backend)
    return [
        ToolSpec(
            name="fs_write",
            description="把文本内容写入任务目录下的文件。mode=overwrite/no_overwrite/new；默认 no_overwrite（不盲目覆盖）。",
            permission="fs",
            timeout=10.0,
            max_calls=50,
            handler=lambda **kw: _write_file(backend=_backend, **kw),
        ),
        ToolSpec(
            name="fs_read",
            description="读取任务目录下指定文件内容。",
            permission="fs",
            timeout=10.0,
            max_calls=100,
            handler=lambda **kw: _read_file(backend=_backend, **kw),
        ),
        ToolSpec(
            name="fs_list",
            description="列出任务目录下（或子目录内）的文件名。",
            permission="fs",
            timeout=10.0,
            max_calls=50,
            handler=lambda **kw: _list_dir(backend=_backend, **kw),
        ),
    ]
