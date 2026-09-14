"""p5-1 artifact versions

Revision ID: f5a6b7c8d901
Revises: d2c7a1b9e00f
Create Date: 2026-09-15

说明（审查🔴/⭐）：
- 新增 ``artifact_versions`` 表承载产物版本链（同一 rel_path 多次写入归档）。
- ``status``：pending → available（失败 failed），驱动「存储+DB 双系统」半状态收敛（🔴1）。
- 唯一约束 ``(task_id, rel_path, version)`` + 版本号 DB 原子递增，保证并发唯一（🔴2）。
- 新增 ``artifact_version_seq`` 序列表：``(task_id, rel_path, next_version)`` 行级原子递增
  生成版本号（``UPDATE ... RETURNING``），并发不重复（🔴2）。
- FK ondelete=CASCADE：任务删除级联清版本记录（🔴5）。
- 非破坏性；downgrade 删表。执行前备份（审查强制）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f5a6b7c8d901'
down_revision: str | Sequence[str] | None = 'd2c7a1b9e00f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'artifact_versions',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('task_id', sa.String(length=36), sa.ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('rel_path', sa.String(length=512), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=512), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sha256', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('mime', sa.String(length=128), nullable=False, server_default='application/octet-stream'),
        sa.Column('producer_role', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('run_id', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('mode', sa.String(length=16), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('task_id', 'rel_path', 'version', name='uq_artifact_versions_task_rel_ver'),
    )
    op.create_index(op.f('ix_artifact_versions_task_id'), 'artifact_versions', ['task_id'], unique=False)
    op.create_index(op.f('ix_artifact_versions_created_at'), 'artifact_versions', ['created_at'], unique=False)

    op.create_table(
        'artifact_version_seq',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('task_id', sa.String(length=36), sa.ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('rel_path', sa.String(length=512), nullable=False),
        sa.Column('next_version', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('task_id', 'rel_path', name='uq_artifact_version_seq_task_rel'),
    )
    op.create_index(op.f('ix_artifact_version_seq_task_id'), 'artifact_version_seq', ['task_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_artifact_version_seq_task_id'), table_name='artifact_version_seq')
    op.drop_table('artifact_version_seq')
    op.drop_index(op.f('ix_artifact_versions_created_at'), table_name='artifact_versions')
    op.drop_index(op.f('ix_artifact_versions_task_id'), table_name='artifact_versions')
    op.drop_table('artifact_versions')
