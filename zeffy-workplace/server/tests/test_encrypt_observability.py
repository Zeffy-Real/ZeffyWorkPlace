"""P6-6-5 加密可观测 · 安全边界与逻辑闭环专项测试（审查🔴 全项）。

覆盖：
1. 权限：普通用户 /admin/governance/encryption-status → 404；匿名 → 404；admin → 200
2. 信息收敛：公共 /metrics 剥离 governance.encryption（get_metrics 白名单剥离）
3. 告警准确性：小流量样本不足不触发；滑动窗口篡改计数；迟滞恢复；冷却
4. 分级：密钥未加载→critical、失败率高→high、集中篡改→high、零星降级→warn
5. 白名单：crypto_metrics / governance_metrics.encryption 不含任何密钥/密文/路径敏感字段
6. 双开关零漂移：总闸关/加密子开关关 → metrics 空、告警不触发、接口 enabled:false
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app
from app.observability import metrics as metrics_mod
from app.storage.local import LocalBackend


@pytest.fixture
async def obs_api(tmp_path):
    s = get_settings()
    from app.storage import reset_backend, set_backend

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    set_backend(LocalBackend(tmp_path))
    from httpx import ASGITransport, AsyncClient

    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac, s
    await ac.__aexit__(None, None, None)
    await eng.dispose()
    reset_backend()
    _restore_global_crypto()


class _FakeUser:
    """模拟鉴权依赖：authenticated/id/is_system/role_is_admin 可控。"""

    def __init__(self, *, authenticated=True, is_system=False, is_admin=False, uid="u1"):
        self.authenticated = authenticated
        self.id = uid if authenticated else None
        self.is_system = is_system
        self._admin = is_admin
        self.role = "admin" if is_admin else "user"

    def role_is_admin(self) -> bool:
        return self._admin


def _enable_crypto_files(tmp_path) -> None:
    from app.storage.crypto_gate import reset_for_test

    m = os.urandom(32)
    h = os.urandom(32)
    (tmp_path / "m1.key").write_bytes(m)
    (tmp_path / "m2.key").write_bytes(m)
    (tmp_path / "hm.key").write_bytes(h)
    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = True
    s.ENCRYPT_MASTER_KEYFILES = f"{tmp_path/'m1.key'},{tmp_path/'m2.key'}"
    s.ENCRYPT_HMAC_KEYFILE = str(tmp_path / "hm.key")
    reset_for_test()


def _restore_global_crypto() -> None:
    """测试完毕恢复全局态：清加密 gate、治理 override、告警态（防跨模块泄漏）。"""
    import app.storage.governance as _gov
    from app.storage.crypto_gate import reset_for_test

    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = False
    s.ENCRYPT_MASTER_KEYFILES = ""
    s.ENCRYPT_HMAC_KEYFILE = ""
    reset_for_test()
    _gov._ovr.pop("meta", None)  # type: ignore[attr-defined]
    metrics_mod._gov_alarm_state.clear()


async def _disable_crypto() -> None:
    from app.storage.crypto_gate import reset_for_test

    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = False
    s.ENCRYPT_MASTER_KEYFILES = ""
    s.ENCRYPT_HMAC_KEYFILE = ""
    reset_for_test()
    metrics_mod._gov_alarm_state.clear()


def _snap(encryption: dict) -> dict:
    """构造告警扫描输入（governance 段，含 encryption）。"""
    return {"encryption": encryption}


# ---- 1. 权限分层（🔴1） ----

@pytest.mark.asyncio
async def test_encryption_status_permissions(obs_api, tmp_path):
    ac, s = obs_api
    from app.auth.deps import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _FakeUser(is_admin=True)
    r = await ac.get("/admin/governance/encryption-status")
    assert r.status_code == 200
    body = r.json()
    assert "health_score" in body and "counters" in body
    assert "key" not in body and "secret" not in str(body).lower()

    app.dependency_overrides[get_current_user] = lambda: _FakeUser(is_admin=False)
    r2 = await ac.get("/admin/governance/encryption-status")
    assert r2.status_code == 404  # 普通用户越权统一 404

    app.dependency_overrides[get_current_user] = lambda: _FakeUser(authenticated=False)
    r3 = await ac.get("/admin/governance/encryption-status")
    assert r3.status_code == 404  # 匿名 404
    app.dependency_overrides.clear()


# ---- 2. 公共 /metrics 信息收敛（🔴1） ----

@pytest.mark.asyncio
async def test_public_metrics_strips_encryption(obs_api):
    metrics_mod._snapshot.update({
        "collected_at": "2026-01-01T00:00:00Z",
        "governance": {"tx_commit": 1, "encryption": {"enabled": True, "counters": {"decrypt_fail": 9}}},
    })
    safe = metrics_mod.get_metrics()
    assert "encryption" not in safe["governance"]  # 公共读剥离
    assert safe["governance"]["tx_commit"] == 1
    # 内部快照仍含（告警扫描用）
    assert "encryption" in metrics_mod._snapshot["governance"]
    metrics_mod._snapshot.clear()
    metrics_mod._snapshot.update({"collected_at": None, "error": "metrics not collected yet"})


# ---- 3. 告警准确性：小流量 / 滑动窗口 / 迟滞 / 冷却（🔴2） ----

def test_encrypt_alarm_low_sample_silent():
    """小流量样本不足 → 不触发失败率告警。"""
    s = get_settings()
    s.ENCRYPT_MIN_SAMPLES = 10
    s.ALERT_ENCRYPT_FAIL_RATE = 0.02
    s.ALERT_ENCRYPT_TAMPER_MIN = 5
    metrics_mod._gov_alarm_state.clear()
    ev = metrics_mod._encrypt_alarm_scan(_snap({
        "enabled": True, "key_loaded": True,
        "counters": {"decrypt": 1, "decrypt_fail": 1},  # 100% 失败但样本<10
        "window": {"decrypt_fail": 1, "tamper": 0, "degrade_plain": 0},
    }), s)
    assert all(e["type"] != "encrypt" or e.get("status") != "triggered"
               for e in ev if e.get("dim") == "encrypt")


def test_encrypt_alarm_fail_rate_high_and_recover():
    """样本充足+失败率超阈 → high 触发；回落 → 恢复（迟滞）。"""
    s = get_settings()
    s.ENCRYPT_MIN_SAMPLES = 10
    s.ALERT_ENCRYPT_FAIL_RATE = 0.02
    s.ALERT_ENCRYPT_TAMPER_MIN = 5
    metrics_mod._gov_alarm_state.clear()
    ev1 = metrics_mod._encrypt_alarm_scan(_snap({
        "enabled": True, "key_loaded": True,
        "counters": {"decrypt": 90, "decrypt_fail": 10},  # 10%
        "window": {"decrypt_fail": 10, "tamper": 0, "degrade_plain": 0},
    }), s)
    hit = [e for e in ev1 if e.get("type") == "encrypt" and e.get("status") == "triggered"
           and e.get("dim") == "encrypt"]
    assert hit and hit[0]["level"] == "high"
    # 回落 → 恢复
    ev2 = metrics_mod._encrypt_alarm_scan(_snap({
        "enabled": True, "key_loaded": True,
        "counters": {"decrypt": 90, "decrypt_fail": 0},
        "window": {"decrypt_fail": 0, "tamper": 0, "degrade_plain": 0},
    }), s)
    rec = [e for e in ev2 if e.get("status") == "recovered" and e.get("dim") == "encrypt"]
    assert rec


def test_encrypt_alarm_tamper_window_and_critical():
    """滑动窗口篡改 ≥ 阈值 → high；密钥未加载 → critical。"""
    s = get_settings()
    s.ALERT_ENCRYPT_TAMPER_MIN = 5
    metrics_mod._gov_alarm_state.clear()
    ev = metrics_mod._encrypt_alarm_scan(_snap({
        "enabled": True, "key_loaded": True,
        "counters": {"decrypt": 100, "decrypt_fail": 0},
        "window": {"decrypt_fail": 0, "tamper": 5, "degrade_plain": 0},
    }), s)
    hit = [e for e in ev if e.get("type") == "encrypt" and e.get("status") == "triggered"]
    assert hit and hit[0]["level"] == "high"
    # 密钥未加载 → critical（即使计数为 0）
    metrics_mod._gov_alarm_state.clear()
    ev2 = metrics_mod._encrypt_alarm_scan(_snap({
        "enabled": True, "key_loaded": False,
        "counters": {"decrypt": 0, "decrypt_fail": 0},
        "window": {"decrypt_fail": 0, "tamper": 0, "degrade_plain": 0},
    }), s)
    crit = [e for e in ev2 if e.get("type") == "encrypt"
            and e.get("status") == "triggered" and e.get("level") == "critical"]
    assert crit


def test_encrypt_alarm_degrade_warn():
    """零星降级明文 → warn 级（不升 high）。"""
    s = get_settings()
    metrics_mod._gov_alarm_state.clear()
    ev = metrics_mod._encrypt_alarm_scan(_snap({
        "enabled": True, "key_loaded": True,
        "counters": {"decrypt": 100, "decrypt_fail": 0},
        "window": {"decrypt_fail": 0, "tamper": 0, "degrade_plain": 1},
    }), s)
    warn = [e for e in ev if e.get("type") == "encrypt"
            and e.get("status") == "triggered" and e.get("level") == "warn"]
    assert warn


def test_encrypt_alarm_disabled_noop():
    """加密关/总闸关 → 告警全 no-op（双开关零漂移）。"""
    s = get_settings()
    metrics_mod._gov_alarm_state.clear()
    ev = metrics_mod._encrypt_alarm_scan(_snap({"enabled": False}), s)
    assert ev == []
    assert metrics_mod._governance_alarm_scan({"governance": {}}) == []


# ---- 4. 白名单输出（🔴4） ----

def test_metrics_whitelist_no_sensitive_fields():
    """crypto_metrics / governance_metrics.encryption 白名单：无密钥/密文/路径。"""
    from app.storage.crypto_gate import crypto_metrics
    from app.storage.governance import governance_metrics

    m = crypto_metrics()
    allowed = {"enabled", "counters", "window", "encrypted_physical_bytes",
               "cipher_version", "key_loaded", "lifecycle"}
    assert set(m.keys()) <= allowed
    enc = (governance_metrics().get("encryption") or {})
    allowed_enc = {"enabled", "key_loaded", "cipher_version", "counters", "window",
                   "lifecycle"}
    assert set(enc.keys()) <= allowed_enc


# ---- 5. 双开关零漂移（🔴5） ----

@pytest.mark.asyncio
async def test_double_switch_zero_drift(obs_api, tmp_path):
    ac, s = obs_api
    from app.auth.deps import get_current_user
    from app.storage.governance import set_governance_override

    app.dependency_overrides[get_current_user] = lambda: _FakeUser(is_admin=True)
    # 加密子开关关
    await _disable_crypto()
    r = await ac.get("/admin/governance/encryption-status")
    assert r.status_code == 200 and r.json()["enabled"] is False
    # 总闸关（加密开）→ 全部短路
    set_governance_override("meta", False)
    _enable_crypto_files(tmp_path)
    from app.storage.crypto_gate import crypt_enabled

    assert crypt_enabled() is False
    r2 = await ac.get("/admin/governance/encryption-status")
    assert r2.json()["enabled"] is False
    set_governance_override("meta", True)
    await _disable_crypto()
    app.dependency_overrides.clear()
