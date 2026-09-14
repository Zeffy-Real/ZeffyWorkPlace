"""p3 user auth + task owner

Revision ID: a3f0c9e1b703
Revises: ee654d8ed570
Create Date: 2026-09-15

说明（审查修订，🔴 断点）：
- 新增 users / user_tokens（token 只存 sha256 哈希，可撤销多 token 并存）。
- tasks 增 owner_id（FK users, 可空）：下行开启鉴权时把历史无主任务归属到内置 system 账号，
  普通用户不可见；普通用户新建任务写 owner。
- 回滚：drop 表 + owner_id 即可，不影响既有数据读取；`alembic downgrade -1`。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f0c9e1b703'
down_revision: str | Sequence[str] | None = 'ee654d8ed570'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 内置 system 账号固定 UUID（承接无主任务）
SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('username'),
    )
    op.create_table(
        'user_tokens',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('token_prefix', sa.String(length=32), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_tokens_token_hash'), 'user_tokens', ['token_hash'], unique=False)
    op.create_index(op.f('ix_user_tokens_user_id'), 'user_tokens', ['user_id'], unique=False)
    op.add_column('tasks', sa.Column('owner_id', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_tasks_owner_id'), 'tasks', ['owner_id'], unique=False)
    op.create_foreign_key('fk_tasks_owner_id_users', 'tasks', 'users',
                          ['owner_id'], ['id'], ondelete='SET NULL')

    # 种子 system 账号（随机盐的占位密码哈希，无需登录态；后端不校验其登录）
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO users (id, email, username, password_hash, is_system, created_at) "
            "VALUES (:id, :email, :uname, :ph, true, now()) "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(
            id=SYSTEM_USER_ID, email="system@zeffy.local",
            uname="system", ph="pbkdf2$1$disabled$system-no-login",
        )
    )
    # 🔴 历史无主任务归属 system（开启鉴权前的老任务可见性）
    bind.execute(
        sa.text("UPDATE tasks SET owner_id = :sys WHERE owner_id IS NULL").bindparams(
            sys=SYSTEM_USER_ID)
    )


def downgrade() -> None:
    op.drop_constraint('fk_tasks_owner_id_users', 'tasks', type_='foreignkey')
    op.drop_index(op.f('ix_tasks_owner_id'), table_name='tasks')
    op.drop_column('tasks', 'owner_id')
    op.drop_index(op.f('ix_user_tokens_token_hash'), table_name='user_tokens')
    op.drop_index(op.f('ix_user_tokens_user_id'), table_name='user_tokens')
    op.drop_table('user_tokens')
    op.drop_table('users')
