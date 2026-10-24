"""message topic for lessons and progress by conversation topic

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("topic", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "topic")
