"""p6-2 智能分层: artifacts.last_access + access_count (按访问频率冷化, P6-2 O1)

Revision ID: a4b5c6d7e004
Revises: a4b5c6d7e003
Create Date: 2026-09-15

说明（P6-2 可选优化 O1 智能分层）：
- 为 ``artifacts`` 增加 ``last_access``（最后完整访问时间，热度埋点依据）与 ``access_count``
  （统计参考，不参与冷化判定）。
- 分块/Range/直链访问不更新 last_access，仅完整文件读取触发。
- 非破坏性（nullable 新列 / 默认 0）；降级仅删列不删数据，支持平滑回滚。
  执行前备份（项目硬约束）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e004'
down_revision: str | Sequence[str] | None = 'a4b5c6d7e003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('artifacts',
                  sa.Column('last_access', sa.DateTime(timezone=True), nullable=True))
    op.add_column('artifacts', sa.Column('access_count', sa.Integer(), nullable=False,
                                         server_default='0'))
    op.create_index(op.f('ix_artifacts_last_access'), 'artifacts', ['last_access'],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_artifacts_last_access'), table_name='artifacts')
    op.drop_column('artifacts', 'access_count')
    op.drop_column('artifacts', 'last_access')
