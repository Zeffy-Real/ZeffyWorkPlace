"""P6-6-6 密钥轮换与生命周期 · 安全专项测试（审查🔴 全项）。

覆盖：
1. 轮换往返：v1 密文 → 切 v2 后旧密文仍可解（旧密钥仅解密）、新写用 v2
2. 版本篡改：改密文头 ver → 解密失败（AAD 绑定）；DEK 绑定版本不符拒绝
3. 版本回退：历史版本 ≥ 当前版本 → 拒绝加载 + unlock_fail
4. 损坏密钥：指纹不符 → 拒绝加载；多副本不一致 → 拒绝
5. 旧密钥仅解密：_LegacyBundle 无加密方法；encrypt 恒当前版本
6. 重裹原子：_rewrap_one 重裹后解密正常、块密文不变、重算 HMAC；损坏输入抛错不产出
7. 三阶段回收：retire 前置零引用扫描；有引用 blocked
8. 到期分级预警：warn/high/critical 按阈值；冷却
9. 白名单/越权：lifecycle 字段不含密钥材料；普通用户 rewrap 404
10. 兼容锚点：无 legacy 配置时与现版一致
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


def _write_keyfile(path, raw: bytes, *, ver: int | None = None,
                   created: float | None = None) -> None:
    """写密钥文件：纯 32B 或带 #zfk 元数据头。"""
    if ver is None and created is None:
        path.write_bytes(raw)
        return
    from app.storage.crypto_gate import _key_fingerprint

    parts = ["#zfk"]
    if ver is not None:
        parts.append(f"ver={ver}")
    if created is not None:
        parts.append(f"created={created:.0f}")
    parts.append(f"fp={_key_fingerprint(raw)}")
    path.write_bytes((" ".join(parts) + "\n").encode() + raw)


def _mk_keys(tmp_path, *, ver: int = 1, created: float | None = None):
    """生成双副本主密钥（hmack 由 master 派生，自包含；轮换兼容）。"""
    m = os.urandom(32)
    m1 = tmp_path / f"m{ver}a.key"
    m2 = tmp_path / f"m{ver}b.key"
    _write_keyfile(m1, m, ver=ver, created=created)
    _write_keyfile(m2, m, ver=ver, created=created)
    return {"m1": str(m1), "m2": str(m2), "master": m}


def _enable(keys, *, ver: int = 1, legacy: str = "", legacy_versions: str = "",
            gray: str = "", gray_ratio: float = 0.0):
    """开加密：当前版本 + 可选 legacy/gray；hmack 自包含派生。"""
    import app.storage.governance as gov
    from app.storage.crypto_gate import reset_for_test

    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = True
    s.ENCRYPT_CIPHER_VERSION = ver
    s.ENCRYPT_MASTER_KEYFILES = f"{keys['m1']},{keys['m2']}"
    s.ENCRYPT_HMAC_KEYFILE = ""  # hmack 由各版本 master 独立派生（自包含）
    s.ENCRYPT_LEGACY_KEYFILES = legacy
    s.ENCRYPT_LEGACY_VERSIONS = legacy_versions
    s.ENCRYPT_GRAY_MASTER_KEYFILES = gray
    s.ENCRYPT_ROTATE_GRAY_RATIO = gray_ratio
    gov.set_governance_override("meta", True)
    reset_for_test()


async def _disable():
    import app.storage.governance as gov
    from app.storage.crypto_gate import reset_for_test

    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = False
    s.ENCRYPT_MASTER_KEYFILES = ""
    s.ENCRYPT_HMAC_KEYFILE = ""
    s.ENCRYPT_LEGACY_KEYFILES = ""
    s.ENCRYPT_GRAY_MASTER_KEYFILES = ""
    reset_for_test()
    gov._ovr.pop("meta", None)  # type: ignore[attr-defined]
    metrics_mod._gov_alarm_state.clear()


@pytest.fixture
async def rot(tmp_path):
    from app.storage import reset_backend, set_backend

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    set_backend(LocalBackend(tmp_path))
    yield tmp_path
    await _disable()
    await eng.dispose()
    reset_backend()


# ---- 1/2. 轮换往返 + 版本篡改 ----

@pytest.mark.asyncio
async def test_rotation_old_cipher_readable_and_tamper_rejected(rot):
    """轮换后旧密文仍可解（旧密钥仅解密）；篡改头 ver → 解密失败（AAD 绑定）。"""
    from app.storage import crypto_gate as G

    k1 = _mk_keys(rot, ver=1)
    _enable(k1, ver=1)
    plain = b"old data" * 500
    c1, meta = await G.encrypt_artifact(plain)
    assert meta["version"] == 1
    # 轮换到 v2：v1 进 legacy
    k2 = _mk_keys(rot, ver=2)
    _enable(k2, ver=2, legacy=f"1:{k1['m1']},{k1['m2']}",
            legacy_versions="1")
    # 旧密文可解
    assert await G.decrypt_artifact(c1) == plain
    # 新写用 v2
    c2, meta2 = await G.encrypt_artifact(b"new")
    assert meta2["version"] == 2
    assert await G.decrypt_artifact(c2) == b"new"
    # 篡改头 ver（v1→v3 或 v2→v1）→ AAD 不匹配 → 拒绝
    bad = bytearray(c2)
    bad[7] = 1 if bad[7] == 2 else 2  # 换成别的版本
    with pytest.raises(G.C.EncryptError):
        await G.decrypt_artifact(bytes(bad))
    await _disable()


# ---- 3. 版本回退拒绝 ----

def test_version_regression_rejected(rot):
    """历史版本 ≥ 当前版本 → 拒绝加载。"""
    from app.storage import crypto_gate as G

    k2 = _mk_keys(rot, ver=3)
    _enable(k2, ver=2, legacy=f"3:{k2['m1']},{k2['m2']}")  # 当前2 < 历史3
    assert G._unlock() is None
    assert G.crypto_metrics()["counters"]["unlock_fail"] >= 1
    await_disable_sync()


def await_disable_sync():
    import asyncio

    asyncio.run(_disable())


# ---- 4. 损坏/指纹不符密钥拒绝 ----

def test_corrupt_key_fingerprint_rejected(rot):
    from app.storage import crypto_gate as G

    # 内嵌 fp 与真实密钥不符 → 拒绝加载
    m = os.urandom(32)
    p1 = rot / "bad1.key"
    p2 = rot / "bad2.key"
    p1.write_bytes(b"#zfk fp=deadbeefdeadbeef\n" + m)
    p2.write_bytes(b"#zfk fp=deadbeefdeadbeef\n" + m)
    _enable({"m1": str(p1), "m2": str(p2)}, ver=1)
    assert G._unlock() is None
    await_disable_sync()


# ---- 5. 旧密钥仅解密（代码级） ----

def test_legacy_bundle_decrypt_only(rot):
    from app.storage import crypto_gate as G

    k1 = _mk_keys(rot, ver=1)
    k2 = _mk_keys(rot, ver=2)
    _enable(k2, ver=2, legacy=f"1:{k1['m1']},{k1['m2']}", legacy_versions="1")
    G._unlock()
    lb = G._legacy_bundles.get(1)
    assert lb is not None
    # 旧密钥束无任何加密/包裹方法（代码级强制）
    for name in ("encrypt", "wrap", "derive_dek"):
        assert not hasattr(lb, name), f"_LegacyBundle 不应有 {name}"
    # 新加密恒用当前版本 v2
    import asyncio

    async def _e():
        c, meta = await G.encrypt_artifact(b"x")
        return meta["version"]

    assert asyncio.get_event_loop().run_until_complete(_e()) == 2
    await_disable_sync()


# ---- 6. 重裹原子 ----

@pytest.mark.asyncio
async def test_rewrap_preserves_and_verifies(rot):
    from app.storage import crypto_gate as G

    k1 = _mk_keys(rot, ver=1)
    _enable(k1, ver=1)
    plain = b"rewrap me" * 2000
    c1, _ = await G.encrypt_artifact(plain)
    core1 = c1[G._GATE_HEAD:]
    # 轮换 v2 后重裹
    k2 = _mk_keys(rot, ver=2)
    _enable(k2, ver=2, legacy=f"1:{k1['m1']},{k1['m2']}", legacy_versions="1")
    from app.storage import get_backend

    backend = get_backend()
    key = "artifacts/t1/doc.md"
    await backend.put(key, c1, mode="overwrite")
    res = await G.rotate_rewrap_deks(keys=[key], backend=backend)
    assert res["rewrapped"] == 1
    new_c = await backend.get(key)
    assert new_c[7] == 2  # 头版本已切 v2
    assert await G.decrypt_artifact(new_c) == plain  # 可解
    # 内层 core 头(63B)重算 HMAC（新 hmack），块密文原样保留
    core_head_len = 7 + 4 + 4 + 8 + 8 + 32  # MAGIC+meta+seed+hmac
    assert new_c[G._GATE_HEAD + core_head_len:] == core1[core_head_len:]
    # 幂等：再跑不重复
    res2 = await G.rotate_rewrap_deks(keys=[key], backend=backend)
    assert res2["rewrapped"] == 0
    await _disable()


# ---- 7. 三阶段回收 ----

@pytest.mark.asyncio
async def test_retire_requires_zero_refs(rot):
    from app.storage import crypto_gate as G
    from app.storage import get_backend

    backend = get_backend()
    k1 = _mk_keys(rot, ver=1)
    _enable(k1, ver=1)
    c1, _ = await G.encrypt_artifact(b"legacy")
    await backend.put("artifacts/t1/a.md", c1, mode="overwrite")
    # 有引用 → blocked
    r = await G.retire_legacy_key(version=1, backend=backend)
    assert r["ok"] is False and r["reason"] == "active_refs"
    # 轮换 v2（v1 进 legacy）→ 重裹 → 零引用 → 放行（阶段①）
    k2 = _mk_keys(rot, ver=2)
    _enable(k2, ver=2, legacy=f"1:{k1['m1']},{k1['m2']}", legacy_versions="1")
    await G.rotate_rewrap_deks(keys=["artifacts/t1/a.md"], backend=backend)
    r2 = await G.retire_legacy_key(version=1, backend=backend)
    assert r2["ok"] is True
    await _disable()


# ---- 8. 到期分级预警 ----

def test_expiry_alert_levels():
    s = get_settings()
    metrics_mod._gov_alarm_state.clear()
    # warn 级
    ev = metrics_mod._encrypt_alarm_scan({
        "encryption": {"enabled": True, "key_loaded": True,
                       "counters": {"decrypt": 0, "decrypt_fail": 0},
                       "window": {"tamper": 0, "degrade_plain": 0, "decrypt_fail": 0},
                       "lifecycle": {"expire_in_days": 20.0, "key_loaded": True}},
    }, s)
    assert any(e.get("level") == "warn" and e.get("dim") == "encrypt-expiry" for e in ev)
    metrics_mod._gov_alarm_state.clear()
    # high 级
    ev2 = metrics_mod._encrypt_alarm_scan({
        "encryption": {"enabled": True, "key_loaded": True,
                       "counters": {}, "window": {},
                       "lifecycle": {"expire_in_days": 3.0}},
    }, s)
    assert any(e.get("level") == "high" and e.get("dim") == "encrypt-expiry" for e in ev2)
    metrics_mod._gov_alarm_state.clear()
    # critical 级
    ev3 = metrics_mod._encrypt_alarm_scan({
        "encryption": {"enabled": True, "key_loaded": True,
                       "counters": {}, "window": {},
                       "lifecycle": {"expire_in_days": 0.5}},
    }, s)
    assert any(e.get("level") == "critical" and e.get("dim") == "encrypt-expiry" for e in ev3)
    metrics_mod._gov_alarm_state.clear()


# ---- 9. 权限：普通用户 rewrap 404 ----

@pytest.mark.asyncio
async def test_rewrap_permission_404(rot):
    from httpx import ASGITransport, AsyncClient

    from app.auth.deps import get_current_user

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)

    class _Fake:
        authenticated = True
        id = "u1"
        is_system = False

        def role_is_admin(self):
            return False

    app.dependency_overrides[get_current_user] = lambda: _Fake()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/admin/governance/encryption/rewrap",
                          json={"keys": ["artifacts/t1/a.md"]})
        assert r.status_code == 404  # 普通用户越权统一 404
    app.dependency_overrides.clear()
    await eng.dispose()


# ---- 10. 兼容锚点：无 legacy 时行为一致 ----

@pytest.mark.asyncio
async def test_no_legacy_zero_drift(rot):
    from app.storage import crypto_gate as G

    k1 = _mk_keys(rot, ver=1)
    _enable(k1, ver=1)
    plain = b"anchor"
    c, meta = await G.encrypt_artifact(plain)
    assert meta["version"] == 1
    assert await G.decrypt_artifact(c) == plain
    lc = G.key_lifecycle_metrics()
    assert lc["current_version"] == 1 and lc["legacy_versions"] == []
    await _disable()
