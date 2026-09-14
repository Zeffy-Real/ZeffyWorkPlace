"""P5 分布式产物存储：后端单例 + 构建/回退 + 指标 + GC（究极审查修订版）。

- ``get_backend()``：进程级单例（配置驱动；local 默认，S3 可选）。
- ``fallback_local()``：S3 运行时故障的本地兜底后端（🔴5 降级重试一次）。
- 存储指标并入可观测体系（⭐4）：put/get/delete 计数 + 字节数 + 错误数。
- GC 守护协程（🔴5 / ⭐5）：清理过期临时文件 + 按任务生命周期清理过期产物。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from app.config import get_settings

# 类型引用（同包子模块；避免局部导入重复）
from app.storage.base import StorageBackend
from app.storage.local import LocalBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 后端单例（配置驱动）
# ---------------------------------------------------------------------------

_backend: StorageBackend | None = None
_fallback_local: LocalBackend | None = None


def build_backend(*, root: str | Path | None = None) -> StorageBackend:
    """按配置构建后端：local（默认）| s3（S3_ENDPOINT 非空且已装 aiobotocore）。

    - ``STORAGE_BACKEND=s3`` 但未配 ``S3_ENDPOINT`` / 未装依赖 → 回退 local 并告警。
    - 显式 ``root``（测试注入）优先。
    """
    s = get_settings()
    use_s3 = s.STORAGE_BACKEND == "s3" and bool(s.S3_ENDPOINT)
    if use_s3:
        from app.storage.s3 import S3Backend

        if not S3Backend.available():
            logger.warning("STORAGE_BACKEND=s3 但未安装 aiobotocore，回退 local 后端")
        else:
            return S3Backend(s)
    root = root or (s.STORAGE_ROOT or s.WORKSPACE_ROOT)
    return LocalBackend(root)


def get_backend() -> StorageBackend:
    """进程级后端单例（首次调用按配置构建）。"""
    global _backend
    if _backend is None:
        _backend = build_backend()
    return _backend


def fallback_local() -> LocalBackend:
    """S3 运行时故障的本地兜底（🔴5：降级重试一次 local）。"""
    global _fallback_local
    if _fallback_local is None:
        s = get_settings()
        _fallback_local = LocalBackend(s.STORAGE_ROOT or s.WORKSPACE_ROOT)
    return _fallback_local


def set_backend(backend: StorageBackend | None) -> None:
    """测试/切换用：替换进程级后端。"""
    global _backend
    _backend = backend


def reset_backend() -> None:
    """清空单例（测试隔离：每用例重新构建）。"""
    global _backend, _fallback_local
    _backend = None
    _fallback_local = None


async def close_backend() -> None:
    global _backend, _fallback_local
    for b in (_backend, _fallback_local):
        if b is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await b.close()
    _backend = None
    _fallback_local = None


# ---------------------------------------------------------------------------
# 存储指标（⭐4：并入可观测；/metrics 聚合展示）
# ---------------------------------------------------------------------------

_metrics: dict[str, Any] = {
    "requests": {"put": 0, "get": 0, "delete": 0, "list": 0},
    "bytes": {"put": 0, "get": 0},
    "errors": {"put": 0, "get": 0, "delete": 0},
    "backend": "local",
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}


def record(kind: str, *, backend: str = "", nbytes: int = 0, error: bool = False) -> None:
    """记录一次存储操作（请求计数/字节/错误）。轻量内存计数，无锁（GIL 内整型自增安全）。"""
    m = _metrics
    m["backend"] = backend or m["backend"]
    if kind in m["requests"]:
        m["requests"][kind] += 1
    if kind in m["bytes"]:
        m["bytes"][kind] += nbytes
    if error and kind in m["errors"]:
        m["errors"][kind] += 1


def storage_metrics() -> dict[str, Any]:
    """返回存储指标快照（并入 /metrics）。"""
    return dict(_metrics)


# ---------------------------------------------------------------------------
# GC 守护协程（🔴5 脏临时文件 + ⭐5 生命周期清理）
# ---------------------------------------------------------------------------

_gc_task: asyncio.Task | None = None


async def cleanup_tmp(backend: StorageBackend) -> int:
    """清理超过 ST_TMP_MAX_AGE 的临时文件（Local: _tmp/*.tmp；S3: artifacts/_tmp/）。"""
    s = get_settings()
    max_age = s.ST_TMP_MAX_AGE
    removed = 0
    if isinstance(backend, LocalBackend):
        tmp_dir = backend.root / "_tmp"
        if tmp_dir.exists():
            for p in tmp_dir.glob("*.tmp"):
                try:
                    if time.time() - p.stat().st_mtime > max_age:
                        p.unlink()
                        removed += 1
                except OSError:  # noqa: BLE001
                    continue
    else:
        try:
            for key in await backend.list("artifacts/_tmp/"):
                removed += 1  # S3 临时 key 由原子写路径兜底清理；此处扫尾
                await backend.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("S3 临时 key 清理失败：%s", exc)
    return removed


async def retention_sweep(session_factory, backend: StorageBackend) -> int:
    """⭐5 生命周期清理：失败任务产物保留 ST_RETENTION_FAILED_DAYS、
    完成任务保留 ST_RETENTION_DONE_DAYS，过期删除。返回清理 key 数。"""
    from datetime import UTC, datetime, timedelta

    from app.db import repos

    s = get_settings()
    now = datetime.now(UTC)
    removed = 0
    try:
        async with session_factory() as session:
            done_cut = now - timedelta(days=s.ST_RETENTION_DONE_DAYS)
            failed_cut = now - timedelta(days=s.ST_RETENTION_FAILED_DAYS)
            for status, cut in (("done", done_cut), ("failed", failed_cut)):
                tasks = await repos.list_old_tasks(session, status=status, updated_before=cut)
                for t in tasks:
                    keys = await backend.list(f"artifacts/{t.id}/")
                    for key in keys:
                        with contextlib.suppress(Exception):  # noqa: BLE001
                            await backend.delete(key)
                            removed += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("产物生命周期清理失败：%s", exc)
    return removed


async def _gc_loop(session_factory, backend: StorageBackend) -> None:
    s = get_settings()
    interval = max(60, s.ST_GARBAGE_INTERVAL)
    while True:
        await asyncio.sleep(interval)
        with contextlib.suppress(Exception):  # noqa: BLE001
            await cleanup_tmp(backend)
        with contextlib.suppress(Exception):  # noqa: BLE001
            await retention_sweep(session_factory, backend)


def start_gc(session_factory, backend: StorageBackend | None = None) -> None:
    """lifespan 启动 GC 后台协程（幂等）。"""
    global _gc_task
    if _gc_task is not None and not _gc_task.done():
        return
    b = backend or get_backend()
    _gc_task = asyncio.create_task(_gc_loop(session_factory, b))


async def stop_gc() -> None:
    global _gc_task
    if _gc_task is not None:
        _gc_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
            await _gc_task
        _gc_task = None
