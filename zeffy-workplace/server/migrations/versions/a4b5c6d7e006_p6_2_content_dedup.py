"""p6-2 O4 内容寻址去重: artifact_content 表 + artifacts.content_ref (P6-2 阶段二)

Revision ID: a4b5c6d7e006
Revises: a4b5c6d7e005
Create Date: 2026-09-15

说明（P6-2 O4 重复数据去重）：
- 新增 ``artifact_content`` 内容寻址表（sha256 PK, size, refs, tier, created_at）；
- ``artifacts`` 增 ``content_ref``(sha256, 可空) 逻辑关联，**不加外键**（避免级联删除风险）；
- 物理 key 由 sha256 确定性推导（``artifacts/_dedup/{sha[:2]}/{sha}``），本迁移不涉及既有数据。
- **降级为破坏性**：downgrade 仅删列（content_ref）与 drop content 表——执行前必须备份；
  正式回滚以 ``DEDUP_ENABLED=false`` 停用代码层逻辑为主，本降级默认不执行。
- `content_ref` 非破坏性新列；执行前备份（项目硬约束）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e006'
down_revision: str | Sequence[str] | None = 'a4b5c6d7e005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'artifact_content',
        sa.Column('sha256', sa.String(64), primary_key=True),
        sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('refs', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tier', sa.String(16), nullable=False, server_default='hot'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('artifacts', sa.Column('content_ref', sa.String(64), nullable=True))


def downgrade() -> None:
    # 破坏性：仅删 content_ref 列 + drop content 表。执行前必须备份。
    op.drop_column('artifacts', 'content_ref')
    op.drop_table('artifact_content')
