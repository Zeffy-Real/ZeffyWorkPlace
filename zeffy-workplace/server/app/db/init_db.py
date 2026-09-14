"""首启建表脚本。

⚠️ 仅 P0 本地开发使用：metadata.create_all 只建不存在的表，不会同步字段变更。
P1 起必须切换 alembic 迁移并废弃本脚本的自动建表。
P0 阶段改模型后需清 volume：docker compose -f docker-compose.base.yml down -v
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models  # noqa: F401  确保模型注册进 Base.metadata
from app.db.base import Base


async def init_db(engine: AsyncEngine) -> None:
    """按当前 metadata 幂等建表。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
