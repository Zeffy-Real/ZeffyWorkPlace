"""P4-1 管理端路由：/admin/cluster 集群视图。

安全（审查🔴）：
- ``ENABLE_ADMIN=false``（默认）→ 一律 404，实例注册仅在后台运行，不暴露端点。
- 仅 ``AUTH_ENABLED`` 且认证为 admin（P4-3 以 role 判定；P4-1 阶段以 system 账号代表）可访问；
  否则统一 404，避免资源枚举。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db import repos
from app.db.base import get_session_factory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class InstanceOut(BaseModel):
    instance_id: str
    kind: str
    host: str
    pid: str | None = None
    started_at: str | None = None
    health: str = "online"
    online: bool = True


class ClusterOut(BaseModel):
    instances: list[InstanceOut]
    counts: dict[str, int]
    generated_at: str


async def _admin_only(user: Annotated[UserPrincipal, Depends(get_current_user)]) -> UserPrincipal:
    s = get_settings()
    if not s.ENABLE_ADMIN:
        raise HTTPException(status_code=404, detail="Not Found")
    if not s.AUTH_ENABLED or not (user.is_system or user.role_is_admin()):
        raise HTTPException(status_code=404, detail="Not Found")
    return user


@router.get("/cluster", response_model=ClusterOut,
            dependencies=[Depends(_admin_only)])
async def cluster(user: Annotated[UserPrincipal, Depends(_admin_only)]) -> ClusterOut:
    import redis.asyncio as aioredis

    from app.observability import instance_reg

    r = aioredis.from_url(get_settings().REDIS_URL)
    try:
        insts = await instance_reg.list_instances(r)
    finally:
        await r.aclose()
    counts: dict[str, int] = {}
    out: list[InstanceOut] = []
    for i in insts:
        key = i.get("kind", "unknown")
        counts[key] = counts.get(key, 0) + 1
        out.append(InstanceOut(
            instance_id=i.get("instance_id", ""),
            kind=i.get("kind", "unknown"),
            host=i.get("host", ""),
            pid=i.get("pid"),
            started_at=i.get("started_at"),
            health=instance_reg.health_level(i),
            online=True,
        ))
    # ⭐ 管理操作全审计：admin/cluster 访问记录 user + trace
    try:
        async with get_session_factory()() as s:
            await repos.write_audit(s, task_id=None, operator="user",
                                    action="admin_cluster_view",
                                    detail={"user_id": user.id, "instances": len(insts)})
    except Exception as exc:  # noqa: BLE001 审计失败不阻断
        logger.warning("admin 审计失败：%s", exc)
    return ClusterOut(instances=out, counts=counts,
                      generated_at=datetime.now(UTC).isoformat())
