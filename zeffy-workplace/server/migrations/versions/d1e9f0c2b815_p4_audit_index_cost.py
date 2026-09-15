"""p4 audit index for billing

Revision ID: d1e9f0c2b815
Revises: b7c4d2f3a90e
Create Date: 2026-09-15

说明（审查🔴）：app 成本聚合按时间窗 + action 过滤 audit_logs 的 JSON（detail 内提取
 usage/model），无索引将全表扫描。加 (created_at, action) 联合索引优化近 30 天聚合。
非破坏性；downgrade 仅删索引。
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd1e9f0c2b815'
down_revision: str | Sequence[str] | None = 'b7c4d2f3a90e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index('ix_audit_logs_created_action', 'audit_logs', ['created_at', 'action'],
                    unique=False)


def downgrade() -> None:
    op.drop_index('ix_audit_logs_created_action', table_name='audit_logs')
