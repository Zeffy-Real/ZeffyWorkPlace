"""p6-closure artifact_tx.reserved_bytes (事务预扣配额, 审查🔴2/🔴6)

Revision ID: a4b5c6d7e003
Revises: a4b5c6d7e002
Create Date: 2026-09-15

说明（P6 审查闭环，🔴2 事务预扣）：
- 为 ``artifact_tx`` 增加 ``reserved_bytes``：tx_open 按 estimated_bytes 原子预扣配额，
  commit 多退少补，rollback/超时 全额返还。默认 0。
- 非破坏性（nullable 默认新列）；downgrade 删列。执行前备份（项目硬约束）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e003'
down_revision: str | Sequence[str] | None = 'a4b5c6d7e002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('artifact_tx', sa.Column('reserved_bytes', sa.Integer(), nullable=False,
                                           server_default='0'))


def downgrade() -> None:
    op.drop_column('artifact_tx', 'reserved_bytes')
