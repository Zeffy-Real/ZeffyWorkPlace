"""P6-6-4 数据加密 · 上层编排门面（接入 put/get/管理链路，补齐剩余 P0）。

在 ``app.storage.crypto``（纯密码学核心）之上提供**自包含加密封装**与编排：

格式化（gate 层信封 + 核心里层）::

    [GATE_MAGIC 7] [ver u8 1] [salt 16] [wrapped_dek 60] [core_blob ...]

- ``core_blob`` 即 ``crypto.encrypt()`` 产物（内部含 nonce seed 头 + 分块 tag），
  头 HMAC 用元数据密钥（hmack）独立校验；块 tag 用每文件 DEK 校验（先验后出）。
- 封套：``salt + wrapped_dek`` 随密文共存，工件**仅凭主密钥即可解封**，
  满足方案「同密钥不同文件 nonce 不重复」与「DEK 存元数据」。

覆盖的剩余 P0：
- **P0-8 密钥权限/副本**：``unlock()`` 从 ``ENCRYPT_MASTER_KEYFILES``（≥2 副本）
  交叉校验主密钥 + HMAC 密钥；进程内缓存，仅本模块持有，不写日志/异常；不一致即拒解锁。
- **P0-4 密文计量**：``crypto_metrics()`` 明/密双口径 + ``cipher_physical_bytes``。
- **P0-9 故障降级**：解锁/加密失败 → 明文 + 告警审计，不阻塞；解密损坏抛错不崩溃。
- **P0-10 加密审计**：``crypto.*`` 全操作审计（解锁/加密/解密/篡改/降级）。
- **P0-6 事务/版本**：自包含密文可整体迁移（._tx/. _v 同后端），无需重加密；
  diff 在解密后明文上做。

总开关：``ARTIFACT_ENCRYPT_ENABLED`` 且治理总闸 ``ARTIFACT_META_ENABLED``；关则全 no-op。
"""

from __future__ import annotations

import logging
import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings
from app.storage import crypto as C

logger = logging.getLogger(__name__)

GATE_MAGIC = b"ZFGATE1"
_GATE_HEAD = 7 + 1 + 16 + 60  # magic + ver + salt + wrapped_dek
_SALT_BYTES = 16


class EncryptConfigError(Exception):
    """密钥配置非法：副本缺失/不一致/HMAC 无法派生（触发故障降级）。"""


class _KeyBundle:
    """内存态密钥束（仅模块持有，绝不外泄）。"""

    __slots__ = ("master", "hmack", "version")

    def __init__(self, master: bytes, hmack: bytes, version: int) -> None:
        self.master = master
        self.hmack = hmack
        self.version = version


_lock: _KeyBundle | None = None
_key_revoked = False

_crypto_counters = {
    "encrypt": 0, "decrypt": 0, "decrypt_fail": 0, "tamper": 0,
    "degrade_plain": 0, "unlock_fail": 0, "unlock_ok": 0,
}
_physical_bytes = 0


def crypto_metrics() -> dict:
    """加密可观测快照（明/密双口径 + 计数）。不泄露任何密钥。"""
    return {
        "enabled": crypt_enabled(),
        "counters": dict(_crypto_counters),
        "encrypted_physical_bytes": _physical_bytes,
        "cipher_version": _lock.version if _lock else None,
        "key_loaded": _lock is not None,
    }


def crypt_enabled() -> bool:
    """总开关：治理总闸 + 加密子开关，任一关闭即否（兼容锚点零漂移）。"""
    s = get_settings()
    if not s.ARTIFACT_ENCRYPT_ENABLED:
        return False
    from app.storage.governance import _enabled

    return bool(_enabled())


def is_encrypted_blob(blob: bytes | None) -> bool:
    """探测 blob 是否为本模块密文（读路径判定）。空/非密文 → False。"""
    return bool(blob and blob.startswith(GATE_MAGIC))


# ---------------- P0-8 · 密钥解锁 / 权限 / 副本校验 ----------------

def _read_keyfile(path: str) -> bytes | None:
    """读取密钥文件：必须 32B（RAW 或 hex/denable 编码）；权限过宽告警。

    文件不存在/超长/非 32B → None（副本校验按缺失处理）。绝不把内容写入日志。
    """
    if not path:
        return None
    try:
        st = os.stat(path)
        if hasattr(st, "st_mode") and (st.st_mode & 0o077):
            logger.warning("密钥文件权限过宽 %o（建议 0600）：%s", st.st_mode & 0o777, path)
        if st.st_size > 4096:
            logger.error("密钥文件异常(超长)：%s", path)
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except (OSError, ValueError):
        return None
    raw = raw.strip()
    if len(raw) == 32:
        return raw
    try:
        decoded = bytes.fromhex(raw.decode("utf-8", "strict"))
        if len(decoded) == 32:
            return decoded
    except (ValueError, UnicodeDecodeError):
        pass
    logger.error("密钥文件内容非 32B（%d）：%s", len(raw), path)
    return None


def _unlock() -> _KeyBundle | None:
    """读取并交叉校验主密钥副本 + HMAC 密钥；任一不过 → None（触发降级）。

    P0-8：≥2 副本全部存在且逐字节一致才解锁。HMAC：优先 ``ENCRYPT_HMAC_KEYFILE``；
    缺省以主密钥独立 HKDF 派生（不同 info），保证「元数据完整性」与「数据」密钥分离。
    """
    global _lock
    if _key_revoked:
        return None
    if _lock is not None:
        return _lock
    if not crypt_enabled():
        return None
    s = get_settings()
    paths = [p.strip() for p in (s.ENCRYPT_MASTER_KEYFILES or "").split(",") if p.strip()]
    if len(paths) < 2:
        _crypto_counters["unlock_fail"] += 1
        logger.error("主密钥副本 < 2，拒绝解锁")
        return None
    masters = [_read_keyfile(p) for p in paths]
    if any(m is None for m in masters):
        _crypto_counters["unlock_fail"] += 1
        logger.error("主密钥存在缺失副本，拒绝解锁")
        return None
    if len({m for m in masters}) != 1:  # noqa: C401  逐字节一致
        _crypto_counters["unlock_fail"] += 1
        logger.error("主密钥副本内容不一致，拒绝解锁（防错钥解密）")
        return None
    master = masters[0]
    hmack = _read_keyfile(s.ENCRYPT_HMAC_KEYFILE or "")
    if hmack is None:
        hmack = HKDF(algorithm=hashes.SHA256(), length=32,
                     salt=b"zeffy-meta-hmac", info=b"meta-hmack").derive(master)
    _lock = _KeyBundle(master=master, hmack=hmack, version=s.ENCRYPT_CIPHER_VERSION)
    _crypto_counters["unlock_ok"] += 1
    return _lock


def revoke_keys() -> None:
    """显式撤销密钥束（安全管理：换钥/应急时清零内存引用）。"""
    global _lock, _key_revoked
    _lock = None
    _key_revoked = True


def reset_for_test() -> None:
    """测试复位：清内存束、撤销位与指标计数。"""
    global _lock, _key_revoked, _physical_bytes
    _lock = None
    _key_revoked = False
    _physical_bytes = 0
    for k in _crypto_counters:
        _crypto_counters[k] = 0


async def _audit_crypto(action: str, *, task_id: str = "", owner_id: str = "",
                        detail: dict | None = None, ok: bool = True, error: str = "") -> None:
    """加密专用审计（P0-10）：并入治理审计通道，action 前缀 crypto.*。"""
    from app.storage.governance import _audit_gov

    await _audit_gov(task_id=task_id, owner_id=owner_id, action=f"crypto.{action}",
                     detail=detail, ok=ok, error=error)


# ---------------- 写路径 · 加密（P0-4 计量 / P0-9 降级） ----------------

async def encrypt_artifact(plain: bytes, *, task_id: str = "", owner_id: str = "") -> tuple[bytes, dict]:
    """明文 → 自包含密文。成功：(cipher, meta{encrypted:True, ...})；失败降级明文。

    故障降级（P0-9）：解锁失败/加密异常 → 返回原始明文 + meta{encrypted:False, reason} +
    严重告警审计，不阻塞写入。计量（P0-4）：meta.cipher_size 密文物理大小。
    """
    global _physical_bytes
    if not crypt_enabled():
        return plain, {"encrypted": False, "plain_size": len(plain),
                       "cipher_size": len(plain), "reason": "crypto_disabled"}
    bundle = _unlock()
    if bundle is None:
        _crypto_counters["degrade_plain"] += 1
        await _audit_crypto("encrypt.degrade", task_id=task_id, owner_id=owner_id,
                            detail={"reason": "unlock_failed"}, ok=False,
                            error="密钥解锁失败，降级明文")
        return plain, {"encrypted": False, "plain_size": len(plain),
                       "cipher_size": len(plain), "reason": "unlock_failed"}
    try:
        salt = os.urandom(_SALT_BYTES)
        dek = C.derive_dek(bundle.master, salt)
        wrapped = C.wrap_dek(bundle.master, dek)
        core = C.encrypt(plain, dek, bundle.hmack)
        head = GATE_MAGIC + struct.pack(">B", bundle.version) + salt + wrapped
        cipher = head + core
        _crypto_counters["encrypt"] += 1
        _physical_bytes += len(cipher)
        await _audit_crypto("encrypt", task_id=task_id, owner_id=owner_id,
                            detail={"plain_size": len(plain), "cipher_size": len(cipher),
                                    "version": bundle.version})
        return cipher, {"encrypted": True, "plain_size": len(plain),
                        "cipher_size": len(cipher), "algo": "AES-256-GCM",
                        "version": bundle.version}
    except Exception as exc:  # noqa: BLE001 降级不阻断
        _crypto_counters["degrade_plain"] += 1
        await _audit_crypto("encrypt.degrade", task_id=task_id, owner_id=owner_id,
                            detail={"reason": "encrypt_error"}, ok=False, error=str(exc))
        logger.exception("加密失败降级明文（task=%s）：%s", task_id, exc)
        return plain, {"encrypted": False, "plain_size": len(plain),
                       "cipher_size": len(plain), "reason": "encrypt_error"}


# ---------------- 读路径 · 解封 + 解密（头校验 / 篡改检测 / 降级语义） ----------------

def _split(cipher: bytes) -> tuple[_KeyBundle, bytes, bytes]:
    """解析 gate 信封 + 解封 DEK → (bundle, core_blob, dek)。失败抛 C.EncryptError。"""
    bundle = _unlock()
    if bundle is None:
        _crypto_counters["decrypt_fail"] += 1
        raise C.EncryptError("密钥未解锁，无法解密")
    if len(cipher) < _GATE_HEAD or not cipher.startswith(GATE_MAGIC):
        _crypto_counters["decrypt_fail"] += 1
        raise C.EncryptError("密文信封头损坏")
    ver = cipher[7]
    s = get_settings()
    legacy = {int(v.strip()) for v in (s.ENCRYPT_LEGACY_VERSIONS or "").split(",") if v.strip()}
    if ver != bundle.version and ver not in legacy:
        _crypto_counters["decrypt_fail"] += 1
        raise C.EncryptError(f"不支持的加密版本：{ver}")
    wrapped = cipher[8 + _SALT_BYTES:_GATE_HEAD]
    dek = C.unwrap_dek(bundle.master, wrapped)  # 主密钥错/损坏 → EncryptError
    return bundle, cipher[_GATE_HEAD:], dek


async def decrypt_artifact(cipher: bytes, *, task_id: str = "", owner_id: str = "") -> bytes:
    """自包含密文 → 明文。损坏/篡改/密钥错误 → 抛 ``C.EncryptError``（API 层转 4xx）。

    P0-10 篡改：头 HMAC / 块 tag / 信封校验失败 → ``crypto.decrypt.tamper`` 审计 + 计数。
    """
    try:
        _bundle, core, dek = _split(cipher)
        plain = C.decrypt_full(core, dek, _bundle.hmack)
        _crypto_counters["decrypt"] += 1
        await _audit_crypto("decrypt", task_id=task_id, owner_id=owner_id,
                            detail={"plain_size": len(plain)})
        return plain
    except C.EncryptError as exc:
        _crypto_counters["decrypt_fail"] += 1
        _crypto_counters["tamper"] += 1
        await _audit_crypto("decrypt.tamper", task_id=task_id, owner_id=owner_id,
                            detail={"reason": str(exc)}, ok=False, error=str(exc))
        raise


async def decrypt_range_artifact(cipher: bytes, *, start: int, end: int | None,
                                 task_id: str = "", owner_id: str = "") -> bytes:
    """Range 解密（P0-2）：gate 解封 → 内部按块对齐解 core，再截 [start, end)。"""
    try:
        _bundle, core, dek = _split(cipher)
        plain = C.decrypt_range(core, dek, _bundle.hmack,
                                start=start, end=end)
        _crypto_counters["decrypt"] += 1
        return plain
    except C.EncryptError as exc:
        _crypto_counters["decrypt_fail"] += 1
        _crypto_counters["tamper"] += 1
        await _audit_crypto("decrypt.tamper", task_id=task_id, owner_id=owner_id,
                            detail={"reason": str(exc)}, ok=False, error=str(exc))
        raise


def peek_plain_size(cipher: bytes) -> int | None:
    """读头明文大小（读路径 Content-Length/进度用）；非密文/未解锁/损坏 → None。"""
    if not is_encrypted_blob(cipher):
        return None
    try:
        _bundle, core, _dek = _split(cipher)
        _block, plen, _seed = C.parse_header(core, _bundle.hmack)
        return plen
    except (C.EncryptError, IndexError):
        return None
