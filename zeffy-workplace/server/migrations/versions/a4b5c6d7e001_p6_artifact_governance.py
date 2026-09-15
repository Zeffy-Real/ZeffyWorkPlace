"""p6-0 artifact governance tables

Revision ID: a4b5c6d7e001
Revises: f5a6b7c8d901
Create Date: 2026-09-15

说明（P6 产物生命周期治理，审查🔴/⭐ 对照）：
- 新增 ``artifacts`` 权威元表：每次写入落一条当前可用产物快照（🔴1）。
  与 ``artifact_versions`` 互补：versions=版本链，artifacts=当前快照；version=0 表示未启用版本化。
  配额/计量/分层/GC 全部以此表为准，杜绝依赖 list() 列举对账。
- 新增 ``quota_usage``：按 owner_id 物化 used_bytes，原子 +/- 维护（🔴3 配额记账）。
- 新增 ``artifact_tx``：事务批次表，驱动多文件原子提交（🔴4）。
- 非破坏性（全部新表+nullable/默认）；downgrade 删表。执行前备份（项目硬约束）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e001'
down_revision: str | Sequence[str] | None = 'f5a6b7c8d901'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 建表顺序：先 artifact_tx / quota_usage（无交叉依赖），再 artifacts（tx_id FK 引用 artifact_tx）。
    op.create_table(
        'artifact_tx',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('task_id', sa.String(length=36), sa.ForeignKey('tasks.id', ondelete='CASCADE'), nullable=True),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('committed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f('ix_artifact_tx_task_id'), 'artifact_tx', ['task_id'], unique=False)
    op.create_index(op.f('ix_artifact_tx_owner_id'), 'artifact_tx', ['owner_id'], unique=False)

    op.create_table(
        'quota_usage',
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'),
                  primary_key=True),
        sa.Column('used_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        'artifacts',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('task_id', sa.String(length=36), sa.ForeignKey('tasks.id', ondelete='CASCADE'), nullable=True),
        sa.Column('rel_path', sa.String(length=512), nullable=False),
        sa.Column('key', sa.String(length=512), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('backend', sa.String(length=16), nullable=False, server_default='local'),
        sa.Column('tier', sa.String(length=16), nullable=False, server_default='hot'),
        sa.Column('sha256', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('mime', sa.String(length=128), nullable=False, server_default='application/octet-stream'),
        sa.Column('producer_role', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='available'),
        sa.Column('tx_id', sa.String(length=36), sa.ForeignKey('artifact_tx.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('task_id', 'rel_path', 'version', name='uq_artifacts_task_rel_ver'),
    )
    op.create_index(op.f('ix_artifacts_task_id'), 'artifacts', ['task_id'], unique=False)
    op.create_index(op.f('ix_artifacts_owner_id'), 'artifacts', ['owner_id'], unique=False)
    op.create_index(op.f('ix_artifacts_created_at'), 'artifacts', ['created_at'], unique=False)
    op.create_index(op.f('ix_artifacts_tx_id'), 'artifacts', ['tx_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_artifacts_tx_id'), table_name='artifacts')
    op.drop_index(op.f('ix_artifacts_created_at'), table_name='artifacts')
    op.drop_index(op.f('ix_artifacts_owner_id'), table_name='artifacts')
    op.drop_index(op.f('ix_artifacts_task_id'), table_name='artifacts')
    op.drop_table('artifacts')
    op.drop_table('quota_usage')
    op.drop_index(op.f('ix_artifact_tx_owner_id'), table_name='artifact_tx')
    op.drop_index(op.f('ix_artifact_tx_task_id'), table_name='artifact_tx')
    op.drop_table('artifact_tx')