"""add slack_event_claims table

Revision ID: c4d8e2f7a1b3
Revises: b3f7c1e05a29
Create Date: 2026-07-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8e2f7a1b3"
down_revision: str | None = "b3f7c1e05a29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slack_event_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_slack_event_claims_event_id"),
    )


def downgrade() -> None:
    op.drop_table("slack_event_claims")
