"""P6-6-4 产物加密 · 上层编排门面专项测试（P0-8 密钥 / P0-4 计量 / P0-9 降级 / P0-10 审计）。

覆盖：
1. 密钥解锁：≥2 副本一致才解锁；缺副本/不一致 → 拒解锁（降级）
2. 加密解密往返：自包含密文仅凭主密钥解封；明/密双口径计量正确
3. 篡改检测：破坏信封 / 内层头 / 密文块 → 解密抛错 + 计数，不返回坏数据
4. 故障降级：未配密钥/解锁失败 → 明文 + reason，不阻断
5. Range 解密：gate 层对齐核心解密正常
6. 兼容锚点：加密关闭 → 原样返回明文，零漂移
"""

from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.storage import crypto_gate as G


@pytest.fixture(autouse=True)
def _clean():
    import app.storage.governance as gov

    G.reset_for_test()
    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = True
    s.ENCRYPT_MASTER_KEYFILES = ""
    s.ENCRYPT_HMAC_KEYFILE = ""
    s.ENCRYPT_LEGACY_VERSIONS = ""
    prior = gov._ovr_get("meta")
    gov.set_governance_override("meta", True)
    yield
    G.reset_for_test()
    s.ARTIFACT_ENCRYPT_ENABLED = False
    if prior is None:
        gov._ovr.pop("meta", None)  # type: ignore[attr-defined, union-attr]
    else:
        gov.set_governance_override("meta", prior)


@pytest.fixture
def keys(tmp_path):
    """写 2 份一致主密钥 + HMAC 密钥 → 返回路径集。"""
    master = os.urandom(32)
    hmack = os.urandom(32)
    m1 = tmp_path / "master1.key"
    m2 = tmp_path / "master2.key"
    hk = tmp_path / "hmac.key"
    m1.write_bytes(master)
    m2.write_bytes(master)
    hk.write_bytes(hmack)
    return {"m1": str(m1), "m2": str(m2), "hk": str(hk)}


def _enable(keys):
    s = get_settings()
    import app.storage.governance as gov

    s.ARTIFACT_ENCRYPT_ENABLED = True
    s.ENCRYPT_MASTER_KEYFILES = f"{keys['m1']},{keys['m2']}"
    s.ENCRYPT_HMAC_KEYFILE = keys["hk"]
    gov.set_governance_override("meta", True)  # 强制覆盖，免受其他模块 config 泄漏影响
    G.reset_for_test()


@pytest.mark.asyncio
async def test_unlock_requires_two_consistent_copies(keys, tmp_path):
    _enable(keys)
    await G.encrypt_artifact(b"x")
    assert G.crypto_metrics()["counters"]["unlock_ok"] == 1
    # 不一致副本 → 拒解锁
    bad = tmp_path / "bad.key"
    bad.write_bytes(os.urandom(32))
    s = get_settings()
    s.ENCRYPT_MASTER_KEYFILES = f"{keys['m1']},{str(bad)}"
    G.reset_for_test()
    await G.encrypt_artifact(b"x")
    assert G.crypto_metrics()["counters"]["unlock_fail"] == 1
    assert G.crypto_metrics()["counters"]["degrade_plain"] == 1
    # 缺副本 → 拒解锁
    s.ENCRYPT_MASTER_KEYFILES = f"{keys['m1']}"
    G.reset_for_test()
    await G.encrypt_artifact(b"x")
    assert G.crypto_metrics()["counters"]["unlock_fail"] == 1


@pytest.mark.asyncio
async def test_roundtrip_and_metering(keys):
    _enable(keys)
    plain = bytes((i * 13) & 0xFF for i in range(500))
    cip, meta = await G.encrypt_artifact(plain, task_id="t1", owner_id="u1")
    assert meta["encrypted"] is True
    assert meta["plain_size"] == len(plain)
    assert len(cip) > len(plain)
    assert meta["cipher_size"] == len(cip)
    out = await G.decrypt_artifact(cip, task_id="t1", owner_id="u1")
    assert out == plain
    m = G.crypto_metrics()
    assert m["encrypted_physical_bytes"] == len(cip)
    assert m["cipher_version"] == 1


@pytest.mark.asyncio
async def test_range_decrypt(keys):
    _enable(keys)
    plain = bytes((i * 7) & 0xFF for i in range(300))
    cip, _ = await G.encrypt_artifact(plain)
    r = await G.decrypt_range_artifact(cip, start=10, end=50)
    assert r == plain[10:50]
    assert G.peek_plain_size(cip) == len(plain)


@pytest.mark.asyncio
async def test_tamper_detected(keys):
    _enable(keys)
    plain = b"secret" * 500
    cip, _ = await G.encrypt_artifact(plain)
    # 破坏信封区（wrapped_dek）→ 解封失败
    bad = bytearray(cip)
    bad[8 + 16 + 4] ^= 0xFF  # wrapped_dek 区域内字节（magic7+ver1+salt16 → wrapped_dek 起点24）
    with pytest.raises(G.C.EncryptError):
        await G.decrypt_artifact(bytes(bad))
    # 破坏内层密文块 → 认证失败
    bad2 = bytearray(cip)
    bad2[-3] ^= 0xFF
    with pytest.raises(G.C.EncryptError):
        await G.decrypt_artifact(bytes(bad2))
    assert G.crypto_metrics()["counters"]["decrypt_fail"] >= 2


@pytest.mark.asyncio
async def test_degrade_when_unlocked_fails():
    # 未配密钥 → 明文降级，不阻断
    plain = b"hello"
    cip, meta = await G.encrypt_artifact(plain, task_id="t1", owner_id="u1")
    assert cip == plain
    assert meta["encrypted"] is False
    assert meta["reason"] == "unlock_failed"


@pytest.mark.asyncio
async def test_disabled_anchor():
    """兼容锚点：加密关闭 → 原样返回明文。"""
    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = True
    from app.storage.governance import set_governance_override

    set_governance_override("meta", False)  # 治理总闸关 → crypt_enabled False
    G.reset_for_test()
    plain = b"anchor"
    cip, meta = await G.encrypt_artifact(plain)
    assert cip == plain and meta["reason"] == "crypto_disabled"
    set_governance_override("meta", True)
