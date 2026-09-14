"""P3-3 token：随机 opaque token + DB 存哈希（可撤销、可审计）。

- 明文 token 形如 ``zwt_<64hex>``（前缀利于日志/排错识别，DB 只存 sha256）。仅颁发时返回一次明文。
- 多 token 并存：每 token 一行，可单独登出撤销；改密时全部历史 token 失效。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from app.config import get_settings

TOKEN_PREFIX = "zwt_"


def generate_token() -> str:
    """生成一条纯明文 token（仅此处可见）。"""
    return f"{TOKEN_PREFIX}{secrets.token_hex(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_prefix_for(token: str, keep: int = 8) -> str:
    """取 token 前 keep 字符做日志/前缀索引，不暴露完整明文。"""
    return token[: max(1, keep)]


def token_ttl_seconds() -> int:
    return get_settings().AUTH_TOKEN_TTL


def expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=token_ttl_seconds())


def is_expired(exp: datetime) -> bool:
    if exp is None:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return datetime.now(UTC) > exp
