"""add review_requested_at column

Revision ID: b6e4d2a8c1f7
Revises: c4d8e2f7a1b3
Create Date: 2026-07-22 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6e4d2a8c1f7"
down_revision: str | None = "c4d8e2f7a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pull_requests",
        sa.Column("review_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pull_requests", "review_requested_at")
