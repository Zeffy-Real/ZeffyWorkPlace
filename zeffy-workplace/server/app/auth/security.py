"""P3-3 密码安全：pbkdf2_hmac_sha256 + 随机盐 + 长度恒定比较。

🔴 强度（审查修订）：迭代 ≥100000（config.PASSWORD_ITERATIONS）、盐 16 字节、
 ``hmac.compare_digest`` 恒定时间比较。存储格式：``pbkdf2$<iter>$<salt_hex>$<hash_hex>``。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import get_settings


def hash_password(password: str) -> str:
    iterations = max(100000, get_settings().PASSWORD_ITERATIONS)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iter_s, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    try:
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected)
