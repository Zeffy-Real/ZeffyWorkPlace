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
from app.storage.versioning import VersionManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 后端单例（配置驱动）
# ---------------------------------------------------------------------------

_backend: StorageBackend | None = None
_fallback_local: LocalBackend | None = None


def build_backend(*, root: str | Path | None = None) -> StorageBackend:
    """按配置构建后端：local（默认）| s3（S3_ENDPOINT 非空且已装 aiobotocore）。

    - ``STORAGE_BACKEND=s3`` 但未配 ``S3_ENDPOINT`` / 未装依赖 → 回退 local 并告警。
    - ``ARTIFACT_VERSIONS_ENABLED=true`` → 用 VersionManager 包装（P5-1 版本链）。
    - 显式 ``root``（测试注入）优先。
    """
    s = get_settings()
    use_s3 = s.STORAGE_BACKEND == "s3" and bool(s.S3_ENDPOINT)
    base: StorageBackend
    if use_s3:
        from app.storage.s3 import S3Backend

        if not S3Backend.available():
            logger.warning("STORAGE_BACKEND=s3 但未安装 aiobotocore，回退 local 后端")
            base = LocalBackend(root or (s.STORAGE_ROOT or s.WORKSPACE_ROOT))
        else:
            base = S3Backend(s)
    else:
        base = LocalBackend(root or (s.STORAGE_ROOT or s.WORKSPACE_ROOT))
    if s.ARTIFACT_VERSIONS_ENABLED:
        return VersionManager(base)
    return base


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
    完成任务保留 ST_RETENTION_DONE_DAYS，过期删除（主产物 + _v 版本 + 表记录，🔴5 级联）。
    返回清理 key 数。"""
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
                    # 🔴5 级联：任务版本存储 + 表记录
                    vkeys = await backend.list(f"artifacts/_v/{t.id}/")
                    for vk in vkeys:
                        with contextlib.suppress(Exception):  # noqa: BLE001
                            await backend.delete(vk)
                            removed += 1
                    async with session_factory() as s2:
                        await repos.delete_version_records_by_task(s2, task_id=t.id)
                    # 🔴5 P6 治理：清理 artifacts 权威元表 + 冲正配额（治理开启时）
                    if get_settings().ARTIFACT_META_ENABLED:
                        await _sweep_governance_task(session_factory, backend, task_id=t.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("产物生命周期清理失败：%s", exc)
    return removed


async def _sweep_governance_task(session_factory, backend: StorageBackend, *, task_id: str) -> None:
    """P6 回收任务级治理元数据：删后端 key + 冲正配额 + 删 artifacts 元表行（🔴5）。

    供 retention_sweep 在任务生命周期到期时联动调用（治理开启时）。
    """
    from app.db import repos

    try:
        async with session_factory() as session:
            rows, _total = await repos.list_artifacts(session, task_id=task_id)
            for a in rows:
                with contextlib.suppress(Exception):  # noqa: BLE001
                    await backend.delete(a.key)
                if a.owner_id:
                    with contextlib.suppress(Exception):  # noqa: BLE001
                        await repos.bump_quota(session, owner_id=a.owner_id, delta=-a.size)
                with contextlib.suppress(Exception):  # noqa: BLE001
                    await repos.delete_artifact_by_key(session, key=a.key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("治理任务级回收失败 task=%s: %s", task_id, exc)


async def version_sweep_once(session_factory, backend: StorageBackend) -> int:
    """🔴1 半状态巡检：清理 pending 超时 / failed 版本记录及其归档对象。返回清理数。"""
    if not isinstance(backend, VersionManager):
        return 0
    from datetime import UTC, datetime, timedelta

    from app.db import repos

    s = get_settings()
    older = datetime.now(UTC) - timedelta(seconds=max(60, s.ARTIFACT_PENDING_TTL))
    cleaned = 0
    try:
        async with session_factory() as session:
            # pending 超时（半状态）与 failed（终态，直接清理）各自巡检
            stale = await repos.stale_version_records(session, status=repos.PENDING,
                                                      older_than=older)
            failed = await repos.stale_version_records(session, status=repos.FAILED,
                                                       older_than=datetime.now(UTC))
            for rec in stale + failed:
                if rec.key:
                    with contextlib.suppress(Exception):  # noqa: BLE001
                        await backend._b.delete(rec.key)
                await repos.delete_version_record(session, record_id=rec.id)
                cleaned += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("版本半状态巡检失败：%s", exc)
    return cleaned


async def version_reconcile_once(session_factory, backend: StorageBackend) -> dict:
    """⭐4 存储与 DB 对账（每日）：DB 有记录存储无文件 → 告警；存储有 _v 文件 DB 无记录 → 清理。"""
    if not isinstance(backend, VersionManager):
        return {"scanned": 0, "missing": 0, "orphans": 0}
    scanned = missing = orphans = 0
    try:
        db_keys = await _all_version_keys(session_factory)
        for key in sorted(db_keys):
            scanned += 1
            exists = await backend._b.exists(key)
            if not exists:
                missing += 1
                logger.warning("对账：DB 有记录但存储缺失 key=%s", key)
        # 存储 → DB：扫描 _v 空间
        try:
            store_keys = await backend._b.list("artifacts/_v/")
        except Exception:  # noqa: BLE001
            store_keys = []
        for sk in store_keys:
            scanned += 1
            if sk not in db_keys:
                orphans += 1
                with contextlib.suppress(Exception):  # noqa: BLE001
                    await backend._b.delete(sk)
                logger.warning("对账：清理孤儿版本对象 key=%s", sk)
    except Exception as exc:  # noqa: BLE001
        logger.warning("版本对账失败：%s", exc)
    return {"scanned": scanned, "missing": missing, "orphans": orphans}


async def _all_version_keys(session_factory) -> set[str]:
    from sqlalchemy import select

    from app.db.models import ArtifactVersion

    keys: set[str] = set()
    async with session_factory() as s:
        rows = (await s.execute(select(ArtifactVersion.key).where(ArtifactVersion.key != ""))).all()
        keys = {r[0] for r in rows}
    return keys


async def _gc_loop(session_factory, backend: StorageBackend) -> None:
    s = get_settings()
    interval = max(60, s.ST_GARBAGE_INTERVAL)
    reconcile_interval = max(3600, s.ARTIFACT_RECONCILE_INTERVAL)
    last_reconcile = 0.0
    while True:
        await asyncio.sleep(interval)
        with contextlib.suppress(Exception):  # noqa: BLE001
            await cleanup_tmp(backend)
        with contextlib.suppress(Exception):  # noqa: BLE001
            await retention_sweep(session_factory, backend)
        # 🔴1 P5-1：pending/failed 半状态巡检
        with contextlib.suppress(Exception):  # noqa: BLE001
            await version_sweep_once(session_factory, backend)
        # 💤 P6 存储分层：治理开启时按年龄自动冷化（元数据标记+真实归档）
        with contextlib.suppress(Exception):  # noqa: BLE001
            from app.storage.governance import cold_sweep_once

            await cold_sweep_once(session_factory, backend)
        # ⭐4 每日对账（仅版本开启时有效）
        now = time.monotonic()
        if now - last_reconcile >= reconcile_interval:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await version_reconcile_once(session_factory, backend)
            last_reconcile = now


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
