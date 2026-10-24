"""feature flags, word upgrades, topic ideas and missions, podcast, referrals, friend pings

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25 01:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("source", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("referred_by", sa.BigInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column("referral_rewarded", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "users", sa.Column("ping_enabled", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.add_column("users", sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("pings_unanswered", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "users", sa.Column("active_mission", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column(
            "hints", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
    )
    # Уже знакомым с ботом подсказки первых дней не нужны.
    op.execute(
        """UPDATE users SET hints = '["first_insight", "level_check", "app_invite", "settings_tip"]'
        WHERE onboarded"""
    )
    op.add_column("vocab", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("vocab", sa.Column("example", sa.Text(), nullable=True))

    op.create_table(
        "feature_flags",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "topic_ideas",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title_ru", sa.Text(), nullable=False),
        sa.Column("title_en", sa.Text(), nullable=True),
        sa.Column("focus_ru", sa.Text(), nullable=True),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_topic_ideas_user_created", "topic_ideas", ["user_id", "created_at"])
    op.create_table(
        "podcasts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("script", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("audio", sa.LargeBinary(), nullable=True),
        sa.Column("mime", sa.Text(), nullable=True),
        sa.Column("chars", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_podcasts_user_created", "podcasts", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_podcasts_user_created", table_name="podcasts")
    op.drop_table("podcasts")
    op.drop_index("ix_topic_ideas_user_created", table_name="topic_ideas")
    op.drop_table("topic_ideas")
    op.drop_table("feature_flags")
    op.drop_column("vocab", "example")
    op.drop_column("vocab", "note")
    for column in (
        "hints",
        "active_mission",
        "pings_unanswered",
        "last_ping_at",
        "ping_enabled",
        "referral_rewarded",
        "referred_by",
        "source",
    ):
        op.drop_column("users", column)
