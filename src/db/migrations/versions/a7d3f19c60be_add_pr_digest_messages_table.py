"""add pr_digest_messages table

Revision ID: a7d3f19c60be
Revises: b6e4d2a8c1f7
Create Date: 2026-08-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3f19c60be"
down_revision: str | None = "b6e4d2a8c1f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pr_digest_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pull_request_id", sa.BigInteger(), nullable=False),
        sa.Column("slack_channel_id", sa.String(length=64), nullable=False),
        sa.Column("slack_ts", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pull_request_id", "slack_channel_id", name="uq_pr_digest_pr_channel"),
    )
    op.create_index(
        op.f("ix_pr_digest_messages_pull_request_id"),
        "pr_digest_messages",
        ["pull_request_id"],
        unique=False,
    )

    # Existing digests were posted to whatever channel the org points at now —
    # that is the only channel the old single-value column could have meant.
    op.execute(
        """
        INSERT INTO pr_digest_messages (pull_request_id, slack_channel_id, slack_ts)
        SELECT pr.id, o.pr_channel_id, pr.pr_digest_ts
        FROM pull_requests AS pr
        JOIN organizations AS o ON o.id = pr.organization_id
        WHERE pr.pr_digest_ts IS NOT NULL
          AND o.pr_channel_id IS NOT NULL
        """
    )
    op.drop_column("pull_requests", "pr_digest_ts")


def downgrade() -> None:
    op.add_column("pull_requests", sa.Column("pr_digest_ts", sa.String(64), nullable=True))
    # Only the current channel's ts fits a single-value column; rows for channels
    # the org has since moved away from are lost.
    op.execute(
        """
        UPDATE pull_requests AS pr
        SET pr_digest_ts = d.slack_ts
        FROM organizations AS o, pr_digest_messages AS d
        WHERE o.id = pr.organization_id
          AND d.pull_request_id = pr.id
          AND d.slack_channel_id = o.pr_channel_id
        """
    )
    op.drop_index(op.f("ix_pr_digest_messages_pull_request_id"), table_name="pr_digest_messages")
    op.drop_table("pr_digest_messages")
