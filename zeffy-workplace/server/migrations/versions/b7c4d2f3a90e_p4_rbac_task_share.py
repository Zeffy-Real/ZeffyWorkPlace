"""p4 rbac role + task share

Revision ID: b7c4d2f3a90e
Revises: a3f0c9e1b703
Create Date: 2026-09-15

说明（审查🔴，破坏性降级提示）：
- users 增 ``role``（admin/user）；system 账号置为 admin。
- 新表 ``task_shares``：viewer/editor 协作分享；仅 owner/admin 可管理（判定在代码层）。
- **downgrade 为破坏性**：删除 role 与 task_shares 会永久丢失分享与角色数据；
  回滚优先用配置关闭权限体系（AUTH_ENABLED=false 即全部放行），确需降级迁移前必须备份。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7c4d2f3a90e'
down_revision: str | Sequence[str] | None = 'a3f0c9e1b703'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('role', sa.String(length=16),
                                     nullable=False, server_default='user'))
    op.create_table(
        'task_shares',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False, server_default='viewer'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'user_id', name='uq_task_share'),
    )
    op.create_index(op.f('ix_task_shares_task_id'), 'task_shares', ['task_id'], unique=False)
    op.create_index(op.f('ix_task_shares_user_id'), 'task_shares', ['user_id'], unique=False)
    # system 账号置为 admin（承接无主任务的系统管理员）
    op.execute("UPDATE users SET role='admin' WHERE is_system=true")


def downgrade() -> None:
    """⚠️ 破坏性：删除 role 与 task_shares，分享/角色数据永久丢失。回滚前请备份。"""
    op.drop_index(op.f('ix_task_shares_user_id'), table_name='task_shares')
    op.drop_index(op.f('ix_task_shares_task_id'), table_name='task_shares')
    op.drop_table('task_shares')
    op.drop_column('users', 'role')
