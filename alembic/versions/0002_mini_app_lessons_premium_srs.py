"""mini app: lessons, premium, srs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24 18:07:45.219784
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audio_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "lessons",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "answers", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=True),
        sa.Column("total", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lessons_user_created", "lessons", ["user_id", "created_at"], unique=False)
    op.add_column("users", sa.Column("premium_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column(
        "vocab", sa.Column("interval_days", sa.REAL(), server_default="0", nullable=False)
    )
    op.add_column("vocab", sa.Column("reps", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("vocab", "reps")
    op.drop_column("vocab", "interval_days")
    op.drop_column("users", "blocked")
    op.drop_column("users", "premium_until")
    op.drop_index("ix_lessons_user_created", table_name="lessons")
    op.drop_table("lessons")
    op.drop_table("audio_cache")
