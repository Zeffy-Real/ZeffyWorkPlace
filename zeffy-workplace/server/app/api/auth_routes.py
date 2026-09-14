"""P3-3 Auth 路由：注册 / 登录 / 登出 / me。

- 注册受 ``REGISTRATION_ENABLED`` 控制（默认 False，防公开滥用）。
- 登录成功签发 opaque token（DB 存哈希）；失败走内存 IP 限流（⭐ 防暴力破解）。
- 撤销：logout 撤当前用户的全部 token（简单可靠，P3 无单 token 登出分离需求）。
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.schemas import LoginIn, RegisterIn, TokenOut, UserOut
from app.auth import security
from app.auth import tokens as tok
from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db import repos
from app.db.base import get_session_factory

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

# ⭐ 登录失败限流：IP -> [失败时间戳...]（内存级；P4 可换 Redis）
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_BAN_SECONDS = 300
_LOGIN_MAX_FAILURES = 5


def _throttle_check(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    fails = [t for t in _LOGIN_FAILURES.get(ip, []) if now - t < _LOGIN_BAN_SECONDS]
    _LOGIN_FAILURES[ip] = fails
    if len(fails) >= _LOGIN_MAX_FAILURES:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {_LOGIN_BAN_SECONDS // 60} 分钟后再试",
        )


def _record_failure(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    _LOGIN_FAILURES.setdefault(ip, []).append(time.time())


async def _issue_token(session, user_id: str) -> dict:
    plain = tok.generate_token()
    expires = tok.expires_at()
    await repos.create_user_token(
        session, user_id=user_id, token_hash=tok.hash_token(plain),
        token_prefix=tok.token_prefix_for(plain), expires_at=expires,
    )
    return {
        "token": plain,
        "token_prefix": tok.token_prefix_for(plain),
        "expires_in": tok.token_ttl_seconds(),
    }


@router.post("/register", response_model=UserOut)
async def register(body: RegisterIn) -> UserOut:
    if not get_settings().REGISTRATION_ENABLED:
        raise HTTPException(status_code=403, detail="公开注册未开启（REGISTRATION_ENABLED）")
    factory = get_session_factory()
    async with factory() as session:
        if await repos.get_user_by_email(session, body.email) is not None:
            raise HTTPException(status_code=409, detail="邮箱已注册")
        if await repos.get_user_by_username(session, body.username) is not None:
            raise HTTPException(status_code=409, detail="用户名已占用")
        user = await repos.create_user(
            session, email=body.email, username=body.username,
            password_hash=security.hash_password(body.password),
        )
        return UserOut.model_validate(user)


@router.post("/login", response_model=TokenOut)
async def login(request: Request, body: LoginIn) -> TokenOut:
    _throttle_check(request)
    factory = get_session_factory()
    user = None
    async with factory() as session:
        user = await repos.get_user_by_email(session, body.email)
        if user is None or not security.verify_password(body.password, user.password_hash):
            _record_failure(request)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="邮箱或密码错误")
        issued = await _issue_token(session, user.id)
        # ⭐ 全量操作审计（user_id + IP）
        await repos.write_audit(session, task_id=None, operator=f"user:{user.id}",
                                action="login", detail={"ip": request.client.host})
    return TokenOut(**issued, user=UserOut.model_validate(user))


@router.post("/logout")
async def logout(request: Request,
                 user: Annotated[UserPrincipal, Depends(get_current_user)]) -> dict:
    if not user.authenticated:
        return {"ok": True}
    factory = get_session_factory()
    async with factory() as session:
        await repos.revoke_all_user_tokens(session, user_id=user.id)
        await repos.write_audit(session, task_id=None, operator=f"user:{user.id}",
                                action="logout",
                                detail={"ip": request.client.host})
    return {"ok": True, "revoked": "all"}


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[UserPrincipal, Depends(get_current_user)]) -> UserOut:
    if not user.authenticated:
        raise HTTPException(status_code=401, detail="未登录")
    factory = get_session_factory()
    async with factory() as session:
        u = await repos.get_user_by_id(session, user.id)
    if u is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return UserOut.model_validate(u)
