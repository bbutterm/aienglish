"""voice mode: auto/always/off instead of voice_always

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24 19:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("voice_mode", sa.Text(), server_default="auto", nullable=False)
    )
    op.execute("UPDATE users SET voice_mode = 'always' WHERE voice_always")
    op.drop_column("users", "voice_always")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("voice_always", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE users SET voice_always = true WHERE voice_mode = 'always'")
    op.drop_column("users", "voice_mode")
