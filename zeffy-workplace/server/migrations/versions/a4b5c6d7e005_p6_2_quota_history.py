"""p6-2 配额智能: quota_history (历史采样, 支撑趋势预测/报表/成本, P6-2 O2)

Revision ID: a4b5c6d7e005
Revises: a4b5c6d7e004
Create Date: 2026-09-15

说明（P6-2 可选优化 O2 配额智能）：
- 新增 ``quota_history`` 采样表：按 owner + 时间点记录 used_bytes，支撑线性趋势预测、
  用量曲线报表与成本核算。
- 从启用日起按 QUOTA_HISTORY_INTERVAL 采样，不回溯历史；GC 按保留窗口清理过期。
- 非破坏性（新增表）；downgrade 删表。执行前备份（项目硬约束）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e005'
down_revision: str | Sequence[str] | None = 'a4b5c6d7e004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'quota_history',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('used_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_quota_history_owner_recorded', 'quota_history',
                    ['owner_id', 'recorded_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_quota_history_owner_recorded', table_name='quota_history')
    op.drop_table('quota_history')
