"""p6-closure artifacts.deleted_at (软删除回收站, 审查🔴5)

Revision ID: a4b5c6d7e002
Revises: a4b5c6d7e001
Create Date: 2026-09-15

说明（P6 审查闭环，🔴5 软删除）：
- 为 ``artifacts`` 增加 ``deleted_at``：软删时置 status='deleted' + deleted_at，
  GC 按 ST_RECYCLE_RETENTION_DAYS 物理删除过期项并释放配额。
- 非破坏性（nullable 新列）；downgrade 删列。执行前备份（项目硬约束）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e002'
down_revision: str | Sequence[str] | None = 'a4b5c6d7e001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('artifacts', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_artifacts_deleted_at'), 'artifacts', ['deleted_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_artifacts_deleted_at'), table_name='artifacts')
    op.drop_column('artifacts', 'deleted_at')
