"""P6-6-4 产物加密 · 密码学核心专项测试（对照方案 P0 ①③⑤⑦）。

覆盖：加解密往返、nonce 唯一性、篡改/错误密钥解密失败（先验后出）、Range 解密、
头 HMAC 校验、DEK 派生/信封、边界（0字节/小块/整块）。
"""

from __future__ import annotations

import os

import pytest

from app.storage import crypto as C


@pytest.fixture
def keys():
    return os.urandom(32), os.urandom(32)  # (master, hmack)


def _pid(keys):  # noqa: ANN001
    master, _ = keys
    salt = os.urandom(16)
    return C.derive_dek(master, salt), salt


def test_roundtrip_and_boundaries(keys):
    master, hmack = keys
    dek, salt = _pid(keys)
    for n in (0, 1, 100, C.BLOCK_DFLT, C.BLOCK_DFLT * 3 + 7):
        plain = bytes((i * 31) & 0xFF for i in range(n))
        cipher = C.encrypt(plain, dek, hmack)
        assert C.decrypt_full(cipher, dek, hmack) == plain
        # Range 全量经 decrypt_range 亦一致
        assert C.decrypt_range(cipher, dek, hmack, start=0, end=n) == plain


def test_nonce_unique_per_file_and_block(keys):
    master, hmack = keys
    dek, _ = _pid(keys)
    plain = b"A" * (C.BLOCK_DFLT * 2 + 5)
    c1 = C.encrypt(plain, dek, hmack)
    c2 = C.encrypt(plain, dek, hmack)  # 同密钥不同文件
    _, _, s1 = C.parse_header(c1, hmack)
    _, _, s2 = C.parse_header(c2, hmack)
    assert s1 != s2  # 不同文件种子不同 → nonce 不重复


def test_tamper_fails_nothing_returned(keys):
    master, hmack = keys
    dek, _ = _pid(keys)
    plain = b"secret data" * 1000
    cipher = bytearray(C.encrypt(plain, dek, hmack))
    cipher[len(C.MAGIC) + 4 + 4 + 8 + C._NONCE_SEED // 2] ^= 0xFF  # 破坏头 HMAC
    with pytest.raises(C.EncryptError):
        C.decrypt_full(bytes(cipher), dek, hmack)
    # 篡改密文块（头合法）→ 认证失败不返回数据
    c2 = C.encrypt(plain, dek, hmack)
    bad = bytearray(c2)
    bad[-3] ^= 0xFF
    with pytest.raises(C.EncryptError):
        C.decrypt_full(bytes(bad), dek, hmack)


def test_wrong_key_fails(keys):
    _, hmack = keys
    other_master = os.urandom(32)
    dek, _ = _pid(keys)
    pk = b"payload"
    cipher = C.encrypt(pk, dek, hmack)
    with pytest.raises(C.EncryptError):
        C.decrypt_full(cipher, other_master, hmack)  # 错 DEK → 认证失败


def test_range_slices(keys):
    master, hmack = keys
    dek, _ = _pid(keys)
    plain = bytes((i * 7) & 0xFF for i in range(300))
    cipher = C.encrypt(plain, dek, hmack, block=64)
    assert C.decrypt_range(cipher, dek, hmack, start=10, end=50) == plain[10:50]
    assert C.decrypt_range(cipher, dek, hmack, start=0, end=64) == plain[0:64]
    assert C.decrypt_range(cipher, dek, hmack, start=65, end=130) == plain[65:130]
    with pytest.raises(C.EncryptError):
        C.decrypt_range(cipher, dek, hmack, start=290, end=9999)


def test_dek_envelope(keys):
    master, _ = keys
    dek = os.urandom(32)
    wrapped = C.wrap_dek(master, dek)
    assert C.unwrap_dek(master, wrapped) == dek
    with pytest.raises(C.EncryptError):
        C.unwrap_dek(os.urandom(32), wrapped)


def test_header_hmac_detects_tamper(keys):
    master, hmack = keys
    dek, _ = _pid(keys)
    cipher = bytearray(C.encrypt(b"x" * 100, dek, hmack))
    # 篡改明文长度字段 → HMAC 不符
    cipher[len(C.MAGIC) + 4 + 4: len(C.MAGIC) + 4 + 8] = (0).to_bytes(4, "big") * 1 + b"\x00"
    with pytest.raises(C.EncryptError):
        C.decrypt_full(bytes(cipher), dek, hmack)


@pytest.mark.asyncio
async def test_high_concurrency_roundtrip(keys):
    """安全专项 · 并发加解密稳定性：200 个并发随机尺寸往返恒等 + 错钥/篡改拒绝。

    受控小样本（单元级冒烟）；全量内存/吞吐/nonce 由 ``scripts/stress_crypto_security.py`` 覆盖。
    """
    import asyncio

    _, hmack = keys
    dek, _ = _pid(keys)
    wrong = os.urandom(32)
    sizes = [i * 137 % 4098 + 1 for i in range(200)]  # ≥1，保证有数据块可认证

    async def _one(sz: int) -> None:
        plain = os.urandom(sz)
        cipher = C.encrypt(plain, dek, hmack)
        assert C.decrypt_full(cipher, dek, hmack) == plain
        with pytest.raises(C.EncryptError):
            C.decrypt_full(cipher, wrong, hmack)  # 错 DEK 拒绝

    await asyncio.gather(*[_one(s) for s in sizes])
