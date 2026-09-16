"""P7-A3 加密头压缩标识（extra 段）· 专项测试。

覆盖版本化扩展头认证核心：
1. extra 段（压缩标识）写入/读取往返，长度 3B
2. 篡改 extra → HMAC 校验失败 → EncryptError（防压缩参数错配）
3. 流式加解密 extra 透传（StreamingEncryptor/Decryptor）
4. 头长度按版本区分（HEAD_VER=62 / HEAD_VER_EXT=65）
5. 非法 extra 长度拒绝
"""
from __future__ import annotations

import os

import pytest

from app.storage import crypto as C


@pytest.fixture
def ctx():
    return {"dek": os.urandom(32), "hmack": os.urandom(32)}


def test_extra_roundtrip(ctx):
    plain = b"compress me payload" * 10
    extra = b"\x01\x00\x06"  # flag=1 gzip level6
    enc = C.encrypt(plain, ctx["dek"], ctx["hmack"], extra=extra)
    _b, _p, _s, got = C.parse_header(enc, ctx["hmack"])
    assert got == extra
    assert C.decrypt_full(enc, ctx["dek"], ctx["hmack"]) == plain


def test_no_extra_ver1_len(ctx):
    head_len1 = len(C.build_header(block=C.BLOCK_DFLT, plaintext_len=1,
                                   seed=b"s" * 8, hmack=ctx["hmack"]))
    head_len2 = len(C.build_header(block=C.BLOCK_DFLT, plaintext_len=1,
                                   seed=b"s" * 8, hmack=ctx["hmack"],
                                   extra=b"\x01\x00\x06"))
    assert head_len2 == head_len1 + C._EXTRA_LEN
    assert head_len2 == C._core_head_len(C.HEAD_VER_EXT)


def test_extra_tamper_fails(ctx):
    """篡改压缩标识（压缩参数错配）→ HMAC 校验失败，拒绝解出乱码。"""
    plain = b"x" * 100
    extra = b"\x01\x00\x06"
    enc = bytearray(C.encrypt(plain, ctx["dek"], ctx["hmack"], extra=extra))
    # 定位 extra 段（MAGIC6 + ver4 + block4 + plen8 + seed8 = 30）
    extra_off = len(C.MAGIC) + 16 + C._NONCE_SEED
    enc[extra_off] = 0  # 改为「未压缩」flag
    with pytest.raises(C.EncryptError):
        C.decrypt_full(bytes(enc), ctx["dek"], ctx["hmack"])
    # 篡改 level 同理
    enc2 = bytearray(C.encrypt(plain, ctx["dek"], ctx["hmack"], extra=extra))
    enc2[extra_off + 2] = 9
    with pytest.raises(C.EncryptError):
        C.decrypt_full(bytes(enc2), ctx["dek"], ctx["hmack"])


def test_build_header_bad_extra_len(ctx):
    with pytest.raises(C.EncryptError):
        C.build_header(block=1, plaintext_len=1, seed=b"s" * 8,
                       hmack=ctx["hmack"], extra=b"\x01\x00")  # 2B 非法


def test_stream_extra_roundtrip(ctx):
    """流式加密带 extra → 流式解密器读回 extra，明文一致。"""
    plain = b"A" * 3000 + b"B" * 500
    enc = C.StreamingEncryptor(ctx["dek"], ctx["hmack"], plaintext_len=len(plain),
                               extra=b"\x01\x00\x06")
    head = enc.header
    payload = bytearray()
    payload += head
    for f in enc.feed(plain[:1500]):
        payload += f
    for f in enc.feed(plain[1500:]):
        payload += f
    payload += enc.finalize()

    dec = C.StreamingDecryptor(ctx["dek"], ctx["hmack"])
    out = bytearray()
    for ct in dec.feed(bytes(payload)):  # 一次喂入（头+块）
        out += ct
    out += dec.finalize()
    assert dec.extra == b"\x01\x00\x06"
    assert bytes(out) == plain


def test_stream_extra_tamper(ctx):
    enc = C.StreamingEncryptor(ctx["dek"], ctx["hmack"], extra=b"\x01\x00\x06")
    head = bytearray(enc.header)
    off = len(C.MAGIC) + 16 + C._NONCE_SEED
    head[off] = 0  # 篡改 flag
    dec = C.StreamingDecryptor(ctx["dek"], ctx["hmack"])
    with pytest.raises(C.EncryptError):
        dec.feed(bytes(head) + b"\x00\x00\x00\x00")


def test_range_decrypt_with_extra(ctx):
    """带 extra 的全量密文 Range 解密仍正确（head_len 动态偏移）。"""
    plain = b"0123456789" * 100  # 1000B
    enc = C.encrypt(plain, ctx["dek"], ctx["hmack"], extra=b"\x01\x00\x06")
    got = C.decrypt_range(enc, ctx["dek"], ctx["hmack"], start=10, end=20)
    assert got == plain[10:20]
