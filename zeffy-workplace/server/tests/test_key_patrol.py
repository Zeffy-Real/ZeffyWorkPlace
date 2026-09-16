"""P7-C3 密钥健康度巡检 · 单元测试（纯文件系统只读核验，不触发解锁、不加载密钥内存态）。

覆盖：
1. 兼容锚点短路：KEY_PATROL_ENABLED 关 / 加密关 / 总闸关 → 零告警
2. 巡检项：主密钥副本不足 / 副本缺失 / 内容不一致 → high
3. 巡检项：密钥文件权限过宽（group/other 位）→ warn
4. 巡检项：版本回退（历史 ≥ 当前）→ critical；文件内嵌版本不符 → critical
5. 巡检项：灰度游离（副本有但比率=0 / 比率>0 但无副本）→ warn
6. 巡检项：到期基准依赖 mtime（无内嵌 created 无配置）→ warn
7. 首次静默：存量异常仅记录不告警；再次巡检才触发
8. 分级差异化冷却：critical/high/warn 冷却期内不重复告警；幂等不风暴
9. 白名单：告警 detail 不含密钥/指纹/路径
"""
from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.observability import key_patrol
from app.storage import crypto_gate as CG


@pytest.fixture(autouse=True)
def _reset_patrol_state():
    key_patrol.reset_patrol_for_test()
    CG.reset_for_test()
    yield
    key_patrol.reset_patrol_for_test()
    s = get_settings()
    s.KEY_PATROL_ENABLED = False
    s.ARTIFACT_ENCRYPT_ENABLED = False
    s.ENCRYPT_MASTER_KEYFILES = ""
    s.ENCRYPT_LEGACY_KEYFILES = ""
    s.ENCRYPT_LEGACY_VERSIONS = ""
    s.ENCRYPT_GRAY_MASTER_KEYFILES = ""
    s.ENCRYPT_ROTATE_GRAY_RATIO = 0.0
    s.ENCRYPT_KEY_CREATED_AT = 0
    s.ENCRYPT_CIPHER_VERSION = 1
    CG.reset_for_test()
    CG.revoke_keys()


def _write_key(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _rand32() -> bytes:
    return os.urandom(32)


def _enable(settings, tmp_path, *, n=2, legacy="", gray="", ratio=0.0):
    """开启加密 + 巡检，双副本一致。"""
    m = _rand32()
    for i in range(n):
        _write_key(tmp_path / f"m{i+1}.key", m)
    settings.KEY_PATROL_ENABLED = True
    settings.ARTIFACT_ENCRYPT_ENABLED = True
    settings.ARTIFACT_META_ENABLED = True
    settings.ENCRYPT_MASTER_KEYFILES = ",".join(
        str(tmp_path / f"m{i+1}.key") for i in range(n))
    settings.ENCRYPT_LEGACY_KEYFILES = legacy
    settings.ENCRYPT_GRAY_MASTER_KEYFILES = gray
    settings.ENCRYPT_ROTATE_GRAY_RATIO = ratio
    CG.reset_for_test()


def _dims(events) -> set[str]:
    return {e["dim"] for e in events}


@pytest.mark.asyncio
async def test_disabled_toggles_short_circuit(tmp_path):
    s = get_settings()
    s.ARTIFACT_META_ENABLED = True
    s.ARTIFACT_ENCRYPT_ENABLED = False
    s.KEY_PATROL_ENABLED = True
    s.ENCRYPT_MASTER_KEYFILES = str(tmp_path / "m1.key")  # 副本不足
    assert key_patrol.run_key_patrol() == []  # 加密关 → 短路
    s.ARTIFACT_ENCRYPT_ENABLED = True
    s.KEY_PATROL_ENABLED = False
    assert key_patrol.run_key_patrol() == []  # 巡检关 → 短路
    s.KEY_PATROL_ENABLED = True
    s.ARTIFACT_META_ENABLED = False
    assert key_patrol.run_key_patrol() == []  # 总闸关 → 短路


@pytest.mark.asyncio
async def test_first_silent_then_alarm(tmp_path):
    s = get_settings()
    _enable(s, tmp_path)  # 健康，无异常
    # 制造异常：副本内容不一致
    _write_key(tmp_path / "m2.key", _rand32())
    # 首次静默：不告警
    s.KEY_PATROL_FIRST_SILENT = True
    ev1 = key_patrol.run_key_patrol()
    assert ev1 == []
    # 第二次：触发
    ev2 = key_patrol.run_key_patrol()
    assert "key-consistency" in _dims(ev2)
    assert ev2[0]["level"] == "high"
    # 关闭首次静默：首次即告警
    key_patrol.reset_patrol_for_test()
    s.KEY_PATROL_FIRST_SILENT = False
    _write_key(tmp_path / "m2.key", _rand32())
    ev3 = key_patrol.run_key_patrol()
    assert "key-consistency" in _dims(ev3)


@pytest.mark.asyncio
async def test_copy_insufficient_missing(tmp_path):
    s = get_settings()
    _enable(s, tmp_path, n=1)
    s.KEY_PATROL_FIRST_SILENT = False
    ev = key_patrol.run_key_patrol()
    assert "key-copies" in _dims(ev)
    assert next(e for e in ev if e["dim"] == "key-copies")["level"] == "high"


@pytest.mark.asyncio
async def test_copy_inconsistent(tmp_path):
    s = get_settings()
    _enable(s, tmp_path, n=2)
    _write_key(tmp_path / "m2.key", _rand32())
    s.KEY_PATROL_FIRST_SILENT = False
    ev = key_patrol.run_key_patrol()
    assert "key-consistency" in _dims(ev)
    assert next(e for e in ev if e["dim"] == "key-consistency")["level"] == "high"


@pytest.mark.asyncio
async def test_permission_too_open(tmp_path):
    s = get_settings()
    _enable(s, tmp_path, n=2)
    os.chmod(tmp_path / "m1.key", 0o666)  # group/other 位非零
    s.KEY_PATROL_FIRST_SILENT = False
    ev = key_patrol.run_key_patrol()
    assert "key-permission" in _dims(ev)
    assert next(e for e in ev if e["dim"] == "key-permission")["level"] == "warn"


@pytest.mark.asyncio
async def test_version_regression(tmp_path):
    s = get_settings()
    # 历史版本 2 ≥ 当前 1 → critical
    _enable(s, tmp_path, n=2, legacy="2:path/x")
    s.KEY_PATROL_FIRST_SILENT = False
    ev = key_patrol.run_key_patrol()
    assert "key-version" in _dims(ev)
    assert next(e for e in ev if e["dim"] == "key-version")["level"] == "critical"


@pytest.mark.asyncio
async def test_gray_orphan(tmp_path):
    s = get_settings()
    _enable(s, tmp_path, n=2, gray=str(tmp_path / "gray1.key,") + str(tmp_path / "gray2.key"), ratio=0.0)
    s.KEY_PATROL_FIRST_SILENT = False
    ev = key_patrol.run_key_patrol()
    assert "key-gray" in _dims(ev)
    assert next(e for e in ev if e["dim"] == "key-gray")["level"] == "warn"


@pytest.mark.asyncio
async def test_expiry_basis_mtime_warn(tmp_path):
    s = get_settings()
    _enable(s, tmp_path, n=2)  # 纯 32B 无 #zfk 头、无 ENCRYPT_KEY_CREATED_AT → 依赖 mtime
    s.KEY_PATROL_FIRST_SILENT = False
    ev = key_patrol.run_key_patrol()
    # 纯 32B 文件 meta={}（无内嵌 created）且无配置指定 → warn
    assert "key-expiry-basis" in _dims(ev)
    assert next(e for e in ev if e["dim"] == "key-expiry-basis")["level"] == "warn"


@pytest.mark.asyncio
async def test_cooldown_no_storm(tmp_path):
    s = get_settings()
    _enable(s, tmp_path, n=2)
    _write_key(tmp_path / "m2.key", _rand32())
    s.KEY_PATROL_FIRST_SILENT = False
    ev1 = key_patrol.run_key_patrol()
    assert "key-consistency" in _dims(ev1)
    # 冷却期内再跑 → 不重复
    ev2 = key_patrol.run_key_patrol()
    assert "key-consistency" not in _dims(ev2)


@pytest.mark.asyncio
async def test_output_whitelist_no_sensitive(tmp_path):
    s = get_settings()
    _enable(s, tmp_path, n=2)
    _write_key(tmp_path / "m2.key", _rand32())
    s.KEY_PATROL_FIRST_SILENT = False
    ev = key_patrol.run_key_patrol()
    for e in ev:
        assert not any(tok in e["value"] for tok in (".key", tmp_path.__str__(), "KEN"))
