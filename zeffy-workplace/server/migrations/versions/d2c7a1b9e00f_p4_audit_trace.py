"""p4 audit trace_id

Revision ID: d2c7a1b9e00f
Revises: e8b5f3d4a901
Create Date: 2026-09-15

说明（审查⭐）：audit_logs 加 ``trace_id``（String64, index），承载全链路 trace
（HTTP/WS 请求 → worker 任务），跨模块（实例/告警/权限/成本/执行）可追溯。
非破坏性；downgrade 删列。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2c7a1b9e00f'
down_revision: str | Sequence[str] | None = 'e8b5f3d4a901'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('audit_logs', sa.Column('trace_id', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_audit_logs_trace_id'), 'audit_logs', ['trace_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_audit_logs_trace_id'), table_name='audit_logs')
    op.drop_column('audit_logs', 'trace_id')
