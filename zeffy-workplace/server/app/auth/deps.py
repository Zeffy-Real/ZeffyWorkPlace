"""P3-3 鉴权依赖：解析 token → 当前用户；AUTH_ENABLED=False 时短路返回匿名。

- ``get_current_user``：FastAPI 依赖，供受保护接口注入。AUTH 关 / 无 token 时按
  ``AUTH_ENABLED`` 决定：开启则 401，关闭则返回匿名（P2 行为完全一致）。
- 返回 ``UserPrincipal``（id 可为 None=匿名/未知归属），owner 过滤据此：后端服务不开
  鉴权时 id=None → 跳过过滤（既有数据 owner_id 不影响）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.db import repos
from app.db.base import get_session_factory

logger = logging.getLogger(__name__)

# 兼容两处携带：Authorization: Bearer；X-Token（前端 WS/简单接入）。
_bearer = HTTPBearer(auto_error=False)

BearerCreds = Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)]


@dataclass
class UserPrincipal:
    id: str | None = None
    username: str | None = None
    is_system: bool = False
    role: str = "user"

    @property
    def authenticated(self) -> bool:
        return self.id is not None

    @property
    def anonymous(self) -> bool:
        return self.id is None

    def role_is_admin(self) -> bool:
        """是否管理员：role==admin 或 system 账号（P4-3）。"""
        return self.is_system or self.role == "admin"


async def get_current_user(creds: BearerCreds = None) -> UserPrincipal:
    """解析当前用户。AUTH 关闭 → 匿名（P2 兼容）；开启 → 验证 token，失败 401。"""
    s = get_settings()
    if not s.AUTH_ENABLED:
        return UserPrincipal(None)  # 匿名：owner 过滤短路 → 行为与 P2 一致
    token = creds.credentials if creds is not None else None
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="缺少 token（Authorization: Bearer）")
    if not token.startswith("zwt_"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token 格式非法")
    from app.auth import tokens as tok

    factory = get_session_factory()
    try:
        async with factory() as session:
            user = await repos.get_user_by_token(session, tok.hash_token(token))
    except Exception as exc:  # noqa: BLE001
        logger.warning("token 校验异常：%s", exc)
        user = None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token 无效或已过期")
    return UserPrincipal(id=user.id, username=user.username, is_system=user.is_system,
                         role=user.role or "user")
