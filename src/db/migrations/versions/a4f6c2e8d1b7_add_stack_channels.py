"""stacked PRs share a channel: non-unique slack_channel_id, slack_thread_ts

Revision ID: a4f6c2e8d1b7
Revises: e7b2f4a16c83
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4f6c2e8d1b7"
down_revision: str | None = "e7b2f4a16c83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_pull_requests_slack_channel_id", table_name="pull_requests")
    op.create_index(
        "ix_pull_requests_slack_channel_id", "pull_requests", ["slack_channel_id"], unique=False
    )
    op.add_column("pull_requests", sa.Column("slack_thread_ts", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("pull_requests", "slack_thread_ts")
    op.drop_index("ix_pull_requests_slack_channel_id", table_name="pull_requests")
    op.create_index(
        "ix_pull_requests_slack_channel_id", "pull_requests", ["slack_channel_id"], unique=True
    )
