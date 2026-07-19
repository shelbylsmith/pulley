"""add message_mappings table

Revision ID: c7e1a2b3d4f5
Revises: 8b7d4e2a9f10
Create Date: 2026-06-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e1a2b3d4f5"
down_revision: str | None = "8b7d4e2a9f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_mappings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pull_request_id", sa.BigInteger(), nullable=False),
        sa.Column("slack_channel_id", sa.String(length=64), nullable=False),
        sa.Column("slack_ts", sa.String(length=64), nullable=False),
        sa.Column("github_comment_id", sa.BigInteger(), nullable=False),
        sa.Column("github_comment_type", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("slack_user_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_message_mappings_pull_request_id"),
        "message_mappings",
        ["pull_request_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_message_mappings_slack_ts"),
        "message_mappings",
        ["slack_ts"],
        unique=False,
    )
    op.create_index(
        op.f("ix_message_mappings_github_comment_id"),
        "message_mappings",
        ["github_comment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_message_mappings_github_comment_id"), table_name="message_mappings")
    op.drop_index(op.f("ix_message_mappings_slack_ts"), table_name="message_mappings")
    op.drop_index(op.f("ix_message_mappings_pull_request_id"), table_name="message_mappings")
    op.drop_table("message_mappings")
