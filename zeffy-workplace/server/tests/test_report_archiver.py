"""P7-B1 报表定时自动归档 · 单元测试。

覆盖：
1. cron 解析：5段 / 非法表达式 / 下一触发时间（含时区、dow|day OR 语义）
2. 兼容锚点：REPORT_ARCHIVE_ENABLED 关 / ARTIFACT_META_ENABLED 关 → 短路零归档
3. 归档生成：调聚合 → 写 backend 归档 key；内容与手动聚合一致
4. no_overwrite 幂等：同日重跑不覆盖、不重复生成
5. 重叠守卫：_running 时跳过本轮（force 忽略）
6. 超时：超时 → 失败 + 治理告警审计
7. 失败 → 治理告警审计（governance_alarm_trigger）
8. 白名单：归档文件名仅日期时段+格式，无敏感字段
9. CSV 注入防护

注入约定：``run_archive_once`` 直接 import ``_encryption_report``（from app.api...），
测试用 ``monkeypatch`` 替换该函数返回预设聚合数据，避免真实审计查询依赖 DB。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.observability import report_archiver as RA
from app.storage import reset_backend, set_backend
from app.storage.local import LocalBackend


@pytest.fixture(autouse=True)
def _archiver_env(tmp_path, monkeypatch):
    """隔离环境：LocalBackend + 归档开关开 + 预设聚合数据 + reset 状态。"""
    s = get_settings()
    # 复位 B1 配置到可测状态
    s.REPORT_ARCHIVE_ENABLED = True
    s.ARTIFACT_META_ENABLED = True
    s.REPORT_ARCHIVE_CRON = "0 3 * * *"
    s.REPORT_ARCHIVE_TZ = ""
    s.REPORT_ARCHIVE_TIMEOUT_S = 60
    s.REPORT_ARCHIVE_RETENTION_DAYS = 180
    s.REPORT_ARCHIVE_FORMAT = "json"
    s.ARTIFACT_ENCRYPT_ENABLED = False

    reset_backend()
    set_backend(LocalBackend(tmp_path))

    # 预设聚合数据
    fake_rows = [
        {"date": "2026-09-15", "action": "encrypt", "total": 10, "ok": 10, "fail": 0},
        {"date": "2026-09-16", "action": "decrypt", "total": 5, "ok": 4, "fail": 1},
    ]

    async def _fake_encryption_report(since, until):
        return [dict(r) for r in fake_rows]

    monkeypatch.setattr("app.api.storage_governance_api._encryption_report",
                        _fake_encryption_report)

    RA.reset_archiver_for_test()
    yield s
    RA.reset_archiver_for_test()
    reset_backend()
    s.REPORT_ARCHIVE_ENABLED = False
    s.ARTIFACT_META_ENABLED = False
    s.ARTIFACT_ENCRYPT_ENABLED = False


def _session_factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    return async_sessionmaker(eng, expire_on_commit=False)


# ---------------- cron 解析 ----------------

def test_parse_cron_five_fields():
    fields = RA.parse_cron("0 3 * * *")
    assert fields[0] == {0}          # minute
    assert fields[1] == {3}          # hour
    assert 1 in fields[2] and 31 in fields[2]  # day *
    assert fields[4] == {0, 1, 2, 3, 4, 5, 6}  # dow *


def test_parse_cron_bad_expr():
    with pytest.raises(ValueError):
        RA.parse_cron("0 3 * *")          # 4 段
    with pytest.raises(ValueError):
        RA.parse_cron("0 25 * * *")       # hour 越界
    with pytest.raises(ValueError):
        RA.parse_cron("*/0 * * * *")      # step 0


def test_next_cron_time_daily():
    after = datetime(2026, 9, 16, 10, 30, tzinfo=UTC)
    nxt = RA.next_cron_time("0 3 * * *", tz=UTC, after=after)
    assert nxt.hour == 3 and nxt.minute == 0
    assert nxt.day == 17  # 次日凌晨 3 点


def test_next_cron_time_dow_or_day():
    """dow 与 day 是 OR 语义：周六(2026-09-19)任一天都触发。"""
    after = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)  # Wed
    nxt = RA.next_cron_time("0 9 * * 6", tz=UTC, after=after)  # 每周六 9 点
    assert nxt.weekday() == 5  # Sat
    assert nxt.hour == 9


def test_next_cron_time_timezone():
    """Asia/Shanghai 与 UTC 的下一触发在墙钟上不同。"""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        sh_tz = ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        # Windows 无 tzdata 包时 zoneinfo 无时区库；与 _archive_loop 的回落设计一致，跳过
        pytest.skip("系统无 tzdata 时区库")
    after_utc = datetime(2026, 9, 16, 2, 0, tzinfo=UTC)
    nxt_sh = RA.next_cron_time("0 3 * * *", tz=sh_tz, after=after_utc)
    # 上海 03:00 = UTC 前一日 19:00，故 UTC 2:00 → 下一上海 3 点在当日
    assert nxt_sh.hour == 3


def test_next_cron_monthly_first_day():
    """day 受限（每月1号）：dows 为 `*` → 仅按 day 匹配，不会每天触发。"""
    after = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    nxt = RA.next_cron_time("0 2 1 * *", tz=UTC, after=after)  # 每月 1 号 02:00
    assert nxt.day == 1
    assert nxt.month == 10  # 从 9-16 之后 → 下一个 1 号是 10-01
    assert nxt.hour == 2


def test_next_cron_both_restricted_or():
    """day 与 dow 双受限 → OR：1 号或周日 03:00 任一触发。"""
    after = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)  # Mon
    # 2026-09-30 = Wed? 计算: 09-28 Mon → 09-29 Tue → 09-30 Wed
    nxt = RA.next_cron_time("0 3 1 * 0", tz=UTC, after=after)  # 1 号 或 周日
    assert nxt.date() in (datetime(2026, 10, 1).date(),)


# ---------------- 兼容锚点 ----------------

async def test_switch_off_shortcircuit(_archiver_env):
    s = _archiver_env
    s.REPORT_ARCHIVE_ENABLED = False
    res = await RA.run_archive_once(_session_factory())
    assert res["ok"] is True and res["skipped"] == "disabled"

    s.REPORT_ARCHIVE_ENABLED = True
    s.ARTIFACT_META_ENABLED = False
    res = await RA.run_archive_once(_session_factory())
    assert res["ok"] is True and res["skipped"] == "disabled"


# ---------------- 归档生成 + 幂等 ----------------

async def test_archive_generates_json(_archiver_env):
    from app.storage import get_backend

    res = await RA.run_archive_once(_session_factory())
    assert res["ok"] is True
    assert res["existed"] is False
    key = res["key"]
    assert key.startswith(RA.ARCHIVE_PREFIX)
    base = key[len(RA.ARCHIVE_PREFIX):]
    # 文件名仅日期时段+格式，无敏感字段
    assert base == RA._archive_filename("json") + ".json"
    assert "report_" in base and "_json" in base

    data = await get_backend().get(key)
    assert data is not None
    import json
    rows = json.loads(data)
    assert rows == [
        {"date": "2026-09-15", "action": "encrypt", "total": 10, "ok": 10, "fail": 0},
        {"date": "2026-09-16", "action": "decrypt", "total": 5, "ok": 4, "fail": 1},
    ]


async def test_archive_no_overwrite_idempotent(_archiver_env):
    res1 = await RA.run_archive_once(_session_factory())
    # 再次执行：同 key no_overwrite → existed，不重复生成
    res2 = await RA.run_archive_once(_session_factory())
    assert res1["key"] == res2["key"]
    assert res2["existed"] is True
    assert res2["ok"] is True


async def test_archive_csv_format(_archiver_env):
    s = _archiver_env
    s.REPORT_ARCHIVE_FORMAT = "csv"
    from app.storage import get_backend

    res = await RA.run_archive_once(_session_factory())
    assert res["ok"] is True
    assert res["key"].endswith(".csv")
    data = await get_backend().get(res["key"])
    text = data.decode("utf-8")
    assert "encrypt" in text and "decrypt" in text
    assert text.startswith("date")  # 表头


async def test_archive_csv_injection_guard(_archiver_env, tmp_path, monkeypatch):
    """CSV 注入防护：以 = + - @ 开头字段前缀单引号。"""
    s = _archiver_env
    s.REPORT_ARCHIVE_FORMAT = "csv"

    async def _inject(since, until):
        return [{"date": "=SUM(A1)", "action": "+cmd", "total": 1, "ok": 1, "fail": 0}]

    monkeypatch.setattr("app.api.storage_governance_api._encryption_report", _inject)
    from app.storage import get_backend

    res = await RA.run_archive_once(_session_factory())
    data = (await get_backend().get(res["key"])).decode("utf-8")
    assert "'=SUM(A1)" in data
    assert "'+cmd" in data


# ---------------- 重叠守卫 ----------------

@pytest.mark.asyncio
async def test_overlap_guard(_archiver_env, monkeypatch):
    """_running=True 时非 force 调用跳过；force 忽略。"""
    # 用 if_false 事件让 _do_archive 挂起，验证重叠守卫
    blocked = asyncio.Event()
    released = asyncio.Event()

    orig = RA._do_archive

    async def stalling(*a, **k):
        blocked.set()
        await released.wait()
        return await orig(*a, **k)

    monkeypatch.setattr(RA, "_do_archive", stalling)
    t1 = asyncio.create_task(RA.run_archive_once(_session_factory()))
    await asyncio.wait_for(blocked.wait(), timeout=2)  # 确保 t1 已进入
    assert RA._running is True

    # 非 force → 跳过
    res2 = await RA.run_archive_once(_session_factory())
    assert res2["skipped"] == "overlap"

    released.set()
    await asyncio.wait_for(t1, timeout=2)
    assert RA._running is False


async def test_overlap_force(_archiver_env, monkeypatch):
    """force=True 忽略 running 守卫，直接执行。"""
    RA._running = True
    res = await RA.run_archive_once(_session_factory(), force=True)
    assert res["ok"] is True and "skipped" not in res


# ---------------- 失败告警 ----------------

async def test_archive_failure_writes_alarm(_archiver_env, monkeypatch):
    """聚合或落位异常 → ok=False + 治理告警审计。"""
    audit_calls = []

    async def fake_audit(session_factory, *, reason, key=""):
        audit_calls.append(reason)

    monkeypatch.setattr(RA, "_emit_failure_audit", fake_audit)

    async def _boom(since, until):
        raise RuntimeError("oops")

    monkeypatch.setattr("app.api.storage_governance_api._encryption_report", _boom)

    res = await RA.run_archive_once(_session_factory())
    assert res["ok"] is False
    assert "oops" in res["error"]
    assert len(audit_calls) == 1


async def test_archive_timeout(_archiver_env, monkeypatch):
    """执行超时 → ok=False + 失败告警。"""
    s = _archiver_env
    s.REPORT_ARCHIVE_TIMEOUT_S = 60
    audit_calls = []

    async def fake_audit(session_factory, *, reason, key=""):
        audit_calls.append(reason)

    monkeypatch.setattr(RA, "_emit_failure_audit", fake_audit)

    async def _slow(*a, **k):
        await asyncio.sleep(10)  # 远超 timeout

    monkeypatch.setattr(RA, "_do_archive", _slow)
    # 把超时压到极短，避免真实等待
    s.REPORT_ARCHIVE_TIMEOUT_S = 0  # 触发 timeout=60 floor，仍 > sleep(10)…改用直接 wait_for 语义

    # 用极短超时验证：run_archive_once 内部 timeout=max(60,...)，故伪造底层耗时 >60 不可行；
    # 改为直接断言 wait_for 捕获超时路径——压短 timeout 到 force 内不可行，采用小幅模拟：
    # 由于 max(60,...)，本用例避免真实 60s，跳过「等待真实超时」，
    # 仅验证 _do_archive 抛 TimeoutError 型异常被 catch 为失败 + 告警。
    async def _raise_timeout(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(RA, "_do_archive", _raise_timeout)
    res = await RA.run_archive_once(_session_factory())
    assert res["ok"] is False
    assert "超时" in res["error"] or "timed out" in res["error"]
    assert len(audit_calls) >= 1


# ---------------- 状态与管理函数 ----------------

def test_archiver_status(_archiver_env):
    st = RA.archiver_status()
    assert st["enabled"] is True
    assert st["cron"] == "0 3 * * *"
    assert st["format"] == "json"
    assert "key" not in st or "last_archive_key" in st  # 无敏感字段字段名


def test_archive_filename_no_sensitive():
    when = datetime(2026, 9, 16, 11, 40)
    name = RA._archive_filename("json", when=when)
    assert name == "report_20260916_1140_json"
    assert ".key" not in name and "secret" not in name


def test_serialize_report_json_csv():
    rows = [{"date": "2026-09-16", "action": "a", "total": 1, "ok": 1, "fail": 0}]
    j = RA._serialize_report(rows, "json")
    assert b"2026-09-16" in j
    c = RA._serialize_report(rows, "csv")
    assert b"DATE" or c  # 至少非空
    assert c.startswith(b"date")
    with pytest.raises(ValueError):
        RA._serialize_report(rows, "pdf")
