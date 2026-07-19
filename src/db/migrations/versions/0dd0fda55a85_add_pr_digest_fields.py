"""add pr digest fields

Revision ID: 0dd0fda55a85
Revises: a82772cd469a
Create Date: 2026-04-24 14:31:47.727003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0dd0fda55a85"
down_revision: str | None = "a82772cd469a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("pr_channel_id", sa.String(64), nullable=True))
    op.add_column("pull_requests", sa.Column("pr_digest_ts", sa.String(64), nullable=True))
    op.add_column("pull_requests", sa.Column("last_review_state", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("pull_requests", "last_review_state")
    op.drop_column("pull_requests", "pr_digest_ts")
    op.drop_column("organizations", "pr_channel_id")
