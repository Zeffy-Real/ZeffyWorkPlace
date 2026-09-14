"""P4-3 权限判定：唯一权限真相（杜绝散点越权）。

权级：view（读）/ edit（写）/ manage_share（分享管理）。
- owner → view+edit+manage_share
- admin（role==admin / system）→ view+edit+manage_share（全任务）
- share viewer → view；share editor → view+edit
- 二次分享禁止：仅 owner/admin 有 manage_share。
- AUTH_ENABLED=false（匿名）→ can_* 恒 True（P3/P2 兼容短路）。
"""

from __future__ import annotations

from typing import Any

from app.auth.deps import UserPrincipal
from app.config import get_settings
from app.db import repos


async def caps_for(session: Any, user: UserPrincipal, task: Any) -> set[str]:
    """返回该用户对 task 的权级（含基础 view；无权限返回空）。"""
    caps: set[str] = set()
    if user.authenticated and (user.is_system or user.role_is_admin()):
        return {"view", "edit", "manage_share"}
    if user.authenticated and task.owner_id == user.id:
        return {"view", "edit", "manage_share"}
    # 共享分享
    share = await repos.get_share(session, task.id, user.id)
    if share:
        caps.add("view")
        if share.role == "editor":
            caps.add("edit")
    return caps


async def can_view(session: Any, user: UserPrincipal, task: Any) -> bool:
    if not user.authenticated:
        return True  # AUTH off → 全部可见（P2）
    return "view" in await caps_for(session, user, task)


async def can_edit(session: Any, user: UserPrincipal, task: Any) -> bool:
    if not user.authenticated:
        return True  # AUTH off → 全部可写（P2 调试 advance 兼容）
    return "edit" in await caps_for(session, user, task)


async def can_manage_share(session: Any, user: UserPrincipal, task: Any) -> bool:
    if not user.authenticated:
        return True
    return "manage_share" in await caps_for(session, user, task)


def auth_off() -> bool:
    return not get_settings().AUTH_ENABLED
