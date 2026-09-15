"""p6-3 N1 分层增强: artifacts.tier_pinned (三级分层+置顶热, 非破坏性)

Revision ID: a4b5c6d7e007
Revises: a4b5c6d7e006
Create Date: 2026-09-15

说明（P6-3 N1 分层增强）：
- 新增 ``artifacts.tier_pinned``(bool default false)：手动置顶热，冷化/backfill 排除。
- 存量默认 false，自动适配现有冷化逻辑，无需数据初始化。
- **非破坏性**：downgrade 仅删列不删数据（按项目硬约束执行前备份；正式回滚以开关降级为主）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e007'
down_revision: str | Sequence[str] | None = 'a4b5c6d7e006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('artifacts',
                  sa.Column('tier_pinned', sa.Boolean(), nullable=False,
                            server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('artifacts', 'tier_pinned')
