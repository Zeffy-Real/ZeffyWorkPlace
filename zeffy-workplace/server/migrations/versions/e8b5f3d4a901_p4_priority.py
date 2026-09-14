"""p4 task priority

Revision ID: e8b5f3d4a901
Revises: d1e9f0c2b815
Create Date: 2026-09-15

说明：tasks 增 ``priority``（0低/1中/2高，默认1），DAG 全节点由代码层继承。
非破坏性；downgrade 仅删列。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e8b5f3d4a901'
down_revision: str | Sequence[str] | None = 'd1e9f0c2b815'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('priority', sa.Integer(), nullable=False,
                                     server_default='1'))
    op.create_index(op.f('ix_tasks_priority'), 'tasks', ['priority'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_tasks_priority'), table_name='tasks')
    op.drop_column('tasks', 'priority')
