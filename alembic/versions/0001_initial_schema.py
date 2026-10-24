"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-24 17:08:20.040611
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("level", sa.Text(), server_default="B1", nullable=False),
        sa.Column("estimated_level", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), server_default="chat", nullable=False),
        sa.Column("voice_always", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("persona", sa.Text(), server_default="us_casual", nullable=False),
        sa.Column("timezone", sa.Text(), server_default="Europe/Moscow", nullable=False),
        sa.Column("ping_time", sa.Time(), nullable=True),
        sa.Column("conversation_summary", sa.Text(), nullable=True),
        sa.Column("context_start_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("onboarded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("streak_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("photo_caption", sa.Text(), nullable=True),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("tts_file_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_messages_user_created",
        "messages",
        ["user_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "usage",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("audio_seconds", sa.REAL(), nullable=True),
        sa.Column("chars", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_user_created", "usage", ["user_id", "created_at"], unique=False)
    op.create_table(
        "vocab",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("translation_ru", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("ease", sa.REAL(), server_default="2.5", nullable=False),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "term", name="uq_vocab_user_term"),
    )
    op.create_table(
        "mistakes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("original", sa.Text(), nullable=False),
        sa.Column("corrected", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("explanation_ru", sa.Text(), nullable=True),
        sa.Column("severity", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("times_seen", sa.Integer(), server_default="1", nullable=False),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mistakes_user_review",
        "mistakes",
        ["user_id", "resolved", "next_review_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mistakes_user_review", table_name="mistakes")
    op.drop_table("mistakes")
    op.drop_table("vocab")
    op.drop_index("ix_usage_user_created", table_name="usage")
    op.drop_table("usage")
    op.drop_index("ix_messages_user_created", table_name="messages")
    op.drop_table("messages")
    op.drop_table("users")
