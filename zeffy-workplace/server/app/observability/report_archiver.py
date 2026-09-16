"""P7-B1 报表定时自动归档 · 低峰 cron 调度 + backend 加密落位 + admin-only 访问。

设计（Stage0 审查通过）：
- **调度**：``REPORT_ARCHIVE_CRON``（5 段 cron，默认 ``0 3 * * *`` 每日 3 点低峰），
  独立协程与 metrics 监控循环解耦，故障不扩散；单轮防重叠（上轮未完跳过本轮）。
- **归档落位**：``artifacts/_governance/report_{YYYYMMDD}_{HHMM}.{json|csv}``，
  经 ``backend.put(mode="no_overwrite")`` 幂等写入；加密开启时先 ``encrypt_artifact``
  再落位（继承产物加密），否则明文 + admin-only 访问。
- **失败告警**：生成/落位/超时失败 → 写 ``governance_alarm_trigger`` 审计（复用治理通道）。
- **兼容锚点**：``REPORT_ARCHIVE_ENABLED=false`` 或 ``ARTIFACT_META_ENABLED=false`` →
  协程不启动 / ``run_archive_once`` 短路，零 IO 零计算。

安全红线：
- 归档内容复用 ``_encryption_report`` 聚合（仅统计维度，无密钥/路径/用户信息）；
- 文件名仅含日期时段 + 格式标识，不含任何敏感字段；
- 读写走 admin-only 接口，越权 404。
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time as _time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 治理归档区 task_id（占位，隔离于普通任务产物，不参与去重/对账）
ARCHIVE_TASK_ID = "_governance"
ARCHIVE_PREFIX = f"artifacts/{ARCHIVE_TASK_ID}/"

# 进程内状态
_archiver_task: asyncio.Task | None = None
_running = False  # 单轮防重叠
_last_run_at: float | None = None
_last_error: str | None = None
_last_archive_key: str | None = None


def reset_archiver_for_test() -> None:
    """测试复位：清空状态标志（不终止协程，由调用方 stop）。"""
    global _running, _last_run_at, _last_error, _last_archive_key
    _running = False
    _last_run_at = None
    _last_error = None
    _last_archive_key = None


# ---------------- 5 段 cron 解析（零依赖，支持 * / N / */N / N,M / N-M） ----------------

_CRON_FIELDS = [
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("dow", 0, 6),  # 0=Sun
]


def _parse_cron_field(expr: str, lo: int, hi: int) -> set[int]:
    """解析单个 cron 字段为合法值集合。支持 * / N / */N / a,b / a-b / a-b/s。"""
    out: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, s = part.split("/", 1)
            step = int(s)
            if step <= 0:
                raise ValueError(f"cron step 非法：{part!r}")
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            v = int(base)
            start = end = v
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron 范围越界：{part!r}（允许 {lo}-{hi}）")
        for v in range(start, end + 1, step):
            out.add(v)
    if not out:
        raise ValueError(f"cron 字段为空：{expr!r}")
    return out


def parse_cron(expr: str) -> list[set[int]]:
    """解析 5 段 cron 表达式 → [minute, hour, day, month, dow] 各值集合。"""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron 必须 5 段：{expr!r}")
    return [_parse_cron_field(p, lo, hi) for (_, lo, hi), p in zip(_CRON_FIELDS, parts, strict=True)]


def next_cron_time(expr: str, *, tz: timezone | None = None,
                   after: datetime | None = None) -> datetime:
    """计算 cron 表达式的下一次触发时间（含 after 之后的第一个匹配）。

    :param tz: 时区；None 用系统本地时区。
    :param after: 基准时间；None = now。
    """
    fields = parse_cron(expr)
    mins, hours, days, months, dows = fields
    if tz is None:
        tz = datetime.now().astimezone().tzinfo or UTC
    now = (after or datetime.now(tz)).astimezone(tz)
    # 未受限判定：字段为全集（`*`）视为「不限制」
    days_free = days == set(range(1, 32))
    dows_free = dows == set(range(0, 7))
    # 从「下一分钟」开始逐分钟扫描，最多扫 366 天（闰年兜底）
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = candidate + timedelta(days=366)
    while candidate <= deadline:
        if not (candidate.minute in mins and candidate.hour in hours
                and candidate.month in months):
            candidate += timedelta(minutes=1)
            continue
        # day/dow 匹配（标准 cron）：仅当两字段都受限时为 OR；任一 `*` 则按另一字段；
        # 两者都 `*` 则每日匹配。cron dow 编号（0=Sun..6=Sat）与 Python weekday()
        # （0=Mon..6=Sun）不同，先转换：cron_dow = (weekday()+1) % 7。
        cron_dow = (candidate.weekday() + 1) % 7
        if days_free and dows_free:
            return candidate
        if days_free:
            day_ok = cron_dow in dows
        elif dows_free:
            day_ok = candidate.day in days
        else:
            day_ok = candidate.day in days or cron_dow in dows
        if day_ok:
            return candidate
        candidate += timedelta(minutes=1)
    raise RuntimeError(f"cron 表达式一年内无匹配：{expr!r}")


# ---------------- 归档核心 ----------------

def _archive_filename(fmt: str, *, when: datetime | None = None) -> str:
    """归档文件名：report_{YYYYMMDD}_{HHMM}_{fmt}。格式标识便于多格式并存。"""
    when = when or datetime.now()
    stamp = when.strftime("%Y%m%d_%H%M")
    return f"report_{stamp}_{fmt}"


def _serialize_report(data: list[dict], fmt: str) -> bytes:
    """把聚合报表序列化为 bytes（json / csv）。"""
    if fmt == "json":
        import json

        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "action", "total", "ok", "fail"])
        for row in data:
            # CSV 注入防护（与 _encryption_report 导出接口一致）
            def _cell(v) -> str:
                s = str(v)
                if s and s[0] in ("=", "+", "-", "@"):
                    s = "'" + s
                return s

            w.writerow([_cell(row["date"]), _cell(row["action"]),
                        row["total"], row["ok"], row["fail"]])
        return buf.getvalue().encode("utf-8")
    raise ValueError(f"不支持的归档格式：{fmt!r}")


async def _emit_failure_audit(session_factory, *, reason: str, key: str = "") -> None:
    """归档失败 → 治理告警审计（复用 governance_alarm_trigger 通道，白名单无敏感）。"""
    from app.db import repos

    try:
        async with session_factory() as s:
            await repos.write_audit(
                s, task_id=None, operator="alert",
                action="governance_alarm_trigger",
                detail={"type": "report-archive", "level": "high",
                        "dim": "report-archive", "value": reason[:200]},
            )
    except Exception as exc:  # noqa: BLE001 告警落库失败不影响主流程
        logger.warning("归档失败审计落库异常：%s", exc)


async def run_archive_once(session_factory, *, force: bool = False) -> dict[str, Any]:
    """执行一轮报表归档。

    :param force: True 时忽略 ``_running`` 重叠守卫（供手动触发/测试）。
    :return: ``{ok, key, bytes, existed}``；失败含 ``error``。
    """
    global _running, _last_run_at, _last_error, _last_archive_key
    s = get_settings()
    # 兼容锚点：总闸 / 归档开关任一关闭 → 短路零开销
    if not s.REPORT_ARCHIVE_ENABLED or not s.ARTIFACT_META_ENABLED:
        return {"ok": True, "skipped": "disabled", "existed": False}

    if _running and not force:
        return {"ok": True, "skipped": "overlap", "existed": False}

    _running = True
    timeout = max(60, int(s.REPORT_ARCHIVE_TIMEOUT_S))
    try:
        result: dict[str, Any] = await asyncio.wait_for(
            _do_archive(session_factory), timeout=timeout)
        _last_run_at = _time.time()
        _last_error = None
        if result.get("key"):
            _last_archive_key = result["key"]
        return result
    except TimeoutError:
        _last_error = f"归档超时（>{timeout}s）"
        logger.error("报表归档超时：%s", _last_error)
        await _emit_failure_audit(session_factory, reason=_last_error)
        return {"ok": False, "error": _last_error, "existed": False}
    except Exception as exc:  # noqa: BLE001 归档全链路异常收敛告警
        _last_error = f"{type(exc).__name__}: {exc}"[:200]
        logger.exception("报表归档失败")
        await _emit_failure_audit(session_factory, reason=_last_error)
        return {"ok": False, "error": _last_error, "existed": False}
    finally:
        _running = False


async def _do_archive(session_factory) -> dict[str, Any]:
    """单轮归档实际执行（不含超时/守卫包装）。"""
    from app.api.storage_governance_api import _encryption_report
    from app.storage import get_backend
    from app.storage.base import FileExistsError_, normalize_artifact_key
    from app.storage.crypto_gate import crypt_enabled, encrypt_artifact

    s = get_settings()
    fmt = s.REPORT_ARCHIVE_FORMAT if s.REPORT_ARCHIVE_FORMAT in ("json", "csv") else "json"
    # 时间窗：默认最近 30 天（与手动导出默认一致），统计口径统一
    from datetime import UTC

    until = datetime.now(UTC).replace(tzinfo=None)
    since = until - timedelta(days=30)
    data = await _encryption_report(since, until)
    payload = _serialize_report(data, fmt)

    fname = _archive_filename(fmt)
    rel_path = f"{fname}.{fmt}"
    key = normalize_artifact_key(ARCHIVE_TASK_ID, rel_path)

    backend = get_backend()
    # 加密开启 → 明文加密后落位（继承产物加密）；owner 留空（治理系统级）
    if crypt_enabled():
        payload, _emeta = await encrypt_artifact(payload, task_id=ARCHIVE_TASK_ID, owner_id="")
    mime = "application/json" if fmt == "json" else "text/csv"
    try:
        meta = await backend.put(key, payload, mode="no_overwrite",
                                 producer_role="report-archiver", mime=mime)
        return {"ok": True, "key": meta.key, "bytes": meta.size,
                "existed": False, "rows": len(data)}
    except FileExistsError_:
        # 幂等：同日同时段已归档 → 跳过（不覆盖、不告警）
        return {"ok": True, "key": key, "existed": True, "rows": len(data)}


def archiver_status() -> dict[str, Any]:
    """归档器运行态快照（admin 只读；无敏感字段）。"""
    s = get_settings()
    return {
        "enabled": bool(s.REPORT_ARCHIVE_ENABLED and s.ARTIFACT_META_ENABLED),
        "cron": s.REPORT_ARCHIVE_CRON,
        "tz": s.REPORT_ARCHIVE_TZ or "system",
        "format": s.REPORT_ARCHIVE_FORMAT,
        "retention_days": s.REPORT_ARCHIVE_RETENTION_DAYS,
        "running": _running,
        "last_run_at": _last_run_at,
        "last_error": _last_error,
        "last_archive_key": _last_archive_key,
    }


# ---------------- 协程生命周期 ----------------

async def _archive_loop(session_factory) -> None:
    """cron 驱动的归档协程：计算下次触发 → sleep → 执行 → 循环。"""
    s = get_settings()
    tz: timezone | None = None
    if s.REPORT_ARCHIVE_TZ:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(s.REPORT_ARCHIVE_TZ)
        except Exception as exc:  # noqa: BLE001 时区非法 → 降级系统时区并告警
            logger.warning("REPORT_ARCHIVE_TZ=%r 无效，降级系统时区：%s",
                           s.REPORT_ARCHIVE_TZ, exc)
            tz = None
    while True:
        try:
            s = get_settings()  # 每次重读（支持运行时配置变更）
            if not (s.REPORT_ARCHIVE_ENABLED and s.ARTIFACT_META_ENABLED):
                await asyncio.sleep(60)
                continue
            nxt = next_cron_time(s.REPORT_ARCHIVE_CRON, tz=tz)
            now = datetime.now(tz or datetime.now().astimezone().tzinfo)
            sleep_s = max(1.0, (nxt - now).total_seconds())
            logger.info("报表归档下次触发：%s（%.0fs 后）", nxt.isoformat(), sleep_s)
            await asyncio.sleep(sleep_s)
            await run_archive_once(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 协程级兜底，绝不让归档异常击穿进程
            logger.exception("报表归档协程异常：%s", exc)
            await asyncio.sleep(300)  # 异常后 5 分钟再试，避免紧循环


def start_archiver(session_factory) -> None:
    """lifespan 启动归档协程（幂等）。开关关闭时不启动（零开销）。"""
    global _archiver_task
    s = get_settings()
    if not (s.REPORT_ARCHIVE_ENABLED and s.ARTIFACT_META_ENABLED):
        logger.info("报表归档未启用（REPORT_ARCHIVE_ENABLED=%s / ARTIFACT_META_ENABLED=%s），跳过启动",
                    s.REPORT_ARCHIVE_ENABLED, s.ARTIFACT_META_ENABLED)
        return
    if _archiver_task is not None and not _archiver_task.done():
        return
    _archiver_task = asyncio.create_task(_archive_loop(session_factory), name="report-archiver")
    logger.info("报表归档协程已启动（cron=%s tz=%s）", s.REPORT_ARCHIVE_CRON,
                s.REPORT_ARCHIVE_TZ or "system")


async def stop_archiver() -> None:
    """lifespan 停止归档协程（幂等）。"""
    global _archiver_task
    if _archiver_task is None:
        return
    if not _archiver_task.done():
        _archiver_task.cancel()
        try:
            await _archiver_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _archiver_task = None
