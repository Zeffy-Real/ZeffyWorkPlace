"""P7-真机回归修复：audit_logs.operator 加宽 32→64（用户 id=36 位 UUID 超长导致写审计 500）。

revision identifiers, used by Alembic.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c5a7d9f210"
down_revision: str | Sequence[str] | None = "a4b5c6d7e007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("audit_logs", "operator", existing_type=sa.String(32), type_=sa.String(64))


def downgrade() -> None:
    op.alter_column("audit_logs", "operator", existing_type=sa.String(64), type_=sa.String(32))