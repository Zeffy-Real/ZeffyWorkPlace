"""P6-6-4 数据加密 · 密码学核心（自包含，不侵入存储后端协议）。

安全红线（对照方案 10 项 P0，本文件覆盖 1/3/5/7 核心）：
- **P0-1 Nonce 唯一性**：每文件 8 字节随机 seed + 块索引(4B) → 12 字节 nonce，同密钥跨文件/同文件跨块不重复。
- **P0-3 先验后出**：`AESGCM.decrypt` 校验 tag 通过才返回明文；失败抛 ``EncryptError``，不返回坏数据。
- **P0-5 头完整性**：密文头带独立 HMAC（元数据密钥派生），解密前先验；篡改即判定损坏。
- **P0-7 边界**：0 字节透传为"无块密文"；小块单块；整块不加空块。

模型：每文件独立 DEK（HKDF：主密钥 + 文件盐 → 32B AES 密钥），DEK 信封用主密钥 AES-GCM 包裹。
默认 ``ARTIFACT_ENCRYPT_ENABLED=false`` 时本模块不被编排层调用（零漂移）。
"""

from __future__ import annotations

import hmac as _std_hmac
import os
import struct
from collections.abc import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac as hmac_mod
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"ZFENC1"
HEAD_VER = 1
BLOCK_DFLT = 262144
_TAG = 16
_NONCE_SEED = 8


class EncryptError(Exception):
    """加密/解密失败（篡改、密钥不匹配、头损坏）。"""


# ---- 密钥派生 / 信封 ----
def derive_dek(master: bytes, salt: bytes) -> bytes:
    """HKDF-SHA256(master, salt) → 32B 每文件 DEK（⭐ 单文件泄露不波及其他）。"""
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=salt, info=b"zeffy-artifact-dek").derive(master)


def wrap_dek(master: bytes, dek: bytes) -> bytes:
    """信封：主密钥 AESGCM 包裹 DEK；nonce 前置存储，解封时取出。"""
    nonce = os.urandom(12)
    return nonce + AESGCM(master).encrypt(nonce, dek, None)


def unwrap_dek(master: bytes, wrapped: bytes) -> bytes:
    try:
        return AESGCM(master).decrypt(wrapped[:12], wrapped[12:], None)
    except InvalidTag as exc:
        raise EncryptError("DEK 封套校验失败（主密钥不匹配）") from exc


# ---- 头 ----
def build_header(*, block: int, plaintext_len: int, seed: bytes, hmack: bytes) -> bytes:
    meta = struct.pack(">IIQ", HEAD_VER, block, plaintext_len) + seed
    tag = _hmac(hmack, meta)
    return MAGIC + meta + tag


def parse_header(cipher: bytes, hmack: bytes) -> tuple[int, int, bytes]:
    """校验并解析头 → (block, plaintext_len, seed)。头损坏/版本不符 → EncryptError。"""
    if len(cipher) < len(MAGIC) + 4 + 4 + 8 + _NONCE_SEED + 32 or not cipher.startswith(MAGIC):
        raise EncryptError("密文头损坏")
    off = len(MAGIC)
    ver, block, plen = struct.unpack(">IIQ", cipher[off:off + 16])
    off += 16
    seed = cipher[off:off + _NONCE_SEED]
    off += _NONCE_SEED
    expected = _hmac(hmack, struct.pack(">IIQ", ver, block, plen) + seed)
    if not _ct_eq(cipher[off:off + 32], expected):
        raise EncryptError("密文头 HMAC 校验失败（元数据被篡改）")
    if ver != HEAD_VER:
        raise EncryptError(f"不支持的加密版本：{ver}")
    return block, plen, seed


def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac_mod.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _ct_eq(a: bytes, b: bytes) -> bool:
    return _std_hmac.compare_digest(a, b)


def _nonce(seed: bytes, idx: int) -> bytes:
    return seed + struct.pack(">I", idx)


# ---- 加密 / 解密 ----
def encrypt(plain: bytes, dek: bytes, hmack: bytes, *, block: int = BLOCK_DFLT) -> bytes:
    """加密纯字节 → 密文（头 + 逐块 [len|ct+tag]）。"""
    seed = os.urandom(_NONCE_SEED)
    out = bytearray(build_header(block=block, plaintext_len=len(plain), seed=seed, hmack=hmack))
    aes = AESGCM(dek)
    for i in range(0, len(plain), block):
        chunk = plain[i:i + block]
        ct = aes.encrypt(_nonce(seed, i // block), chunk, None)
        out += struct.pack(">I", len(ct)) + ct
    return bytes(out)


def _iter_blocks(cipher: bytes):
    off = len(MAGIC) + 4 + 4 + 8 + _NONCE_SEED + 32
    while off < len(cipher):
        if off + 4 > len(cipher):
            raise EncryptError("密文块长度头越界")
        (ln,) = struct.unpack(">I", cipher[off:off + 4])
        off += 4
        if ln < 1 or off + ln > len(cipher):
            raise EncryptError("密文块损坏")
        yield off, cipher[off:off + ln]
        off += ln


def decrypt_full(cipher: bytes, dek: bytes, hmack: bytes, *, block: int = BLOCK_DFLT) -> bytes:
    _block, _plen, seed = parse_header(cipher, hmack)
    aes = AESGCM(dek)
    out = bytearray()
    for idx, (_off, ct) in enumerate(_iter_blocks(cipher)):
        try:
            out += aes.decrypt(_nonce(seed, idx), ct, None)  # 先验后出（P0-3）
        except InvalidTag as exc:
            raise EncryptError(f"块 {idx} 认证失败（篡改/密钥错误），已丢弃") from exc
    return bytes(out)


def decrypt_range(cipher: bytes, dek: bytes, hmack: bytes, *,
                  start: int, end: int | None, block: int = BLOCK_DFLT) -> bytes:
    """Range 解密：对齐到块内部解，再按用户 [start,end) 截取（P0-2）。"""
    block, plen, seed = parse_header(cipher, hmack)
    end = end if end is not None else plen
    if start < 0 or end < start or end > plen:
        raise EncryptError("Range 越界")
    aes = AESGCM(dek)
    out = bytearray()
    first = start // block
    last = (max(start, end) - 1) // block
    for idx, (_off, ct) in enumerate(_iter_blocks(cipher)):
        if idx < first or idx > last:
            continue
        try:
            pt = aes.decrypt(_nonce(seed, idx), ct, None)
        except InvalidTag as exc:
            raise EncryptError(f"块 {idx} 认证失败") from exc
        lo = max(0, start - idx * block)
        hi = min(len(pt), end - idx * block)
        if hi > lo:
            out += pt[lo:hi]
    return bytes(out)


# ===========================================================================
# P7 前收尾 · 流式加解密（P0-1：边读边解、内存常量级，大文件不整载入内存）
# ===========================================================================

_CORE_HEAD_LEN = len(MAGIC) + 4 + 4 + 8 + _NONCE_SEED + 32  # MAGIC+meta+seed+hmac = 63


class StreamingEncryptor:
    """分块流式加密器：`feed(plain)` 产出完整块密文帧，`finalize()` 产出尾块。

    内存峰值 = 单块 + 头（常量级）。头先经 ``header`` 属性输出；
    ``plaintext_len=None`` 时头内长度记 0（解密流式忽略 plen，Range 需调用方已知）。
    """

    __slots__ = ("_aes", "_block", "_idx", "_buf", "_done", "_seed", "_header")

    def __init__(self, dek: bytes, hmack: bytes, *, block: int = BLOCK_DFLT,
                 plaintext_len: int | None = None) -> None:
        self._seed = os.urandom(_NONCE_SEED)
        self._aes = AESGCM(dek)
        self._block = block
        self._idx = 0
        self._buf = b""
        self._done = False
        self._header = build_header(block=block,
                                    plaintext_len=plaintext_len or 0,
                                    seed=self._seed, hmack=hmack)

    @property
    def header(self) -> bytes:
        """密文头（先输出）。"""
        return self._header

    def feed(self, plain: bytes) -> list[bytes]:
        """喂入明文块，返回完整块密文帧列表（``[len|ct+tag]``）；不满一块缓存。"""
        out: list[bytes] = []
        if self._done or not plain:
            return out
        self._buf += plain
        while len(self._buf) >= self._block:
            chunk, self._buf = self._buf[:self._block], self._buf[self._block:]
            ct = self._aes.encrypt(_nonce(self._seed, self._idx), chunk, None)
            self._idx += 1
            out.append(struct.pack(">I", len(ct)) + ct)
        return out

    def finalize(self) -> bytes:
        """产出尾块帧（可能为空）；此后不再接受 feed。"""
        if self._done:
            return b""
        self._done = True
        if self._buf:
            ct = self._aes.encrypt(_nonce(self._seed, self._idx), self._buf, None)
            self._idx += 1
            self._buf = b""
            return struct.pack(">I", len(ct)) + ct
        return b""

    def close(self) -> None:
        """提前丢弃缓冲（异常路径资源释放）。"""
        self._buf = b""
        self._done = True


class StreamingDecryptor:
    """分块流式解密器：先验头 HMAC → 逐块先验 tag 后出明文（P0-3）。

    支持 Range：``start/end``（明文坐标）时跳过起始块前帧、对目标块截取。
    内存峰值 = 单块密文 + 缓冲（常量级）。
    """

    __slots__ = ("_dek", "_hmack", "_buf", "_aes", "_seed", "_block", "_plen",
                 "_idx", "_start", "_end", "_state")

    def __init__(self, dek: bytes, hmack: bytes, *, start: int = 0,
                 end: int | None = None) -> None:
        self._dek = dek
        self._hmack = hmack
        self._buf = b""
        self._aes = None
        self._seed = None
        self._block = 0
        self._plen = 0
        self._idx = 0
        self._start = max(0, start)
        self._end = end
        self._state = "head"  # head -> blocks -> done

    def feed(self, cipher: bytes) -> list[bytes]:
        """喂入密文，返回已解密的完整明文块列表。头校验失败/块认证失败抛 EncryptError。"""
        out: list[bytes] = []
        self._buf += cipher
        while True:
            if self._state == "head":
                if len(self._buf) < _CORE_HEAD_LEN:
                    break
                head, self._buf = self._buf[:_CORE_HEAD_LEN], self._buf[_CORE_HEAD_LEN:]
                _ver, block, plen, seed = _parse_core_head(head, self._hmack)
                self._block = block
                self._plen = plen
                self._seed = seed
                self._aes = AESGCM(self._dek)
                self._idx = 0
                self._state = "blocks"
                continue
            if self._state == "blocks":
                if len(self._buf) < 4:
                    break
                (ln,) = struct.unpack(">I", self._buf[:4])
                if ln < 1 or len(self._buf) < 4 + ln:
                    if len(self._buf) - 4 >= 4:  # 已有完整块长但密文帧未齐 → 等待更多
                        break
                    break
                frame, self._buf = self._buf[:4 + ln], self._buf[4 + ln:]
                ct = frame[4:]
                # Range 跳过起始块前帧
                if self._idx * self._block + self._block <= self._start:
                    self._idx += 1
                    continue
                try:
                    pt = self._aes.decrypt(_nonce(self._seed, self._idx), ct, None)  # 先验后出
                except InvalidTag as exc:
                    raise EncryptError(f"块 {self._idx} 认证失败（篡改/密钥错误），已丢弃") from exc
                # Range 截取
                lo = max(0, self._start - self._idx * self._block)
                hi = self._plen
                if self._end is not None:
                    hi = min(len(pt), self._end - self._idx * self._block)
                if hi > lo:
                    out.append(pt[lo:hi])
                self._idx += 1
                if self._end is not None and self._idx * self._block >= self._end:
                    self._state = "done"
                    break
                continue
            break
        return out

    def finalize(self) -> bytes:
        """收尾：非 Range 全量时校验无未解密帧残留；返回剩余明文（通常为空）。"""
        if self._state == "head":
            raise EncryptError("密文流缺失（头部未完整）")
        if self._state == "blocks":
            if self._buf:
                raise EncryptError("密文流结尾不完整（残留帧）")
            self._state = "done"
        return b""


def _parse_core_head(head: bytes, hmack: bytes) -> tuple[int, int, int, bytes]:
    """解析内层 core 头（流式用）：返回 (ver, block, plen, seed)；校验 HMAC。"""
    if not head.startswith(MAGIC):
        raise EncryptError("密文头损坏")
    ver, block, plen = struct.unpack(">IIQ", head[len(MAGIC):len(MAGIC) + 16])
    seed = head[len(MAGIC) + 16:len(MAGIC) + 16 + _NONCE_SEED]
    expected = _hmac(hmack, struct.pack(">IIQ", ver, block, plen) + seed)
    if not _ct_eq(head[len(MAGIC) + 16 + _NONCE_SEED:], expected):
        raise EncryptError("密文头 HMAC 校验失败（元数据被篡改）")
    if ver != HEAD_VER:
        raise EncryptError(f"不支持的加密版本：{ver}")
    return ver, block, plen, seed


async def decrypt_stream(cipher_iter, dek: bytes, hmack: bytes, *,
                         start: int = 0, end: int | None = None):
    """异步流式解密：逐块喂入 → 逐块 yield 明文（先验后出）。内存常量级。"""
    d = StreamingDecryptor(dek, hmack, start=start, end=end)
    async for chunk in cipher_iter:
        for pt in d.feed(chunk):
            yield pt
    tail = d.finalize()
    if tail:
        yield tail


def encrypt_enabled() -> bool:
    from app.config import get_settings
    from app.storage.governance import _enabled

    s = get_settings()
    return bool(_enabled() and s.ARTIFACT_ENCRYPT_ENABLED)


def _noop(_: Iterable[bytes]) -> Iterable[bytes]:
    return _
