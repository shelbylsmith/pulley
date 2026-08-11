"""add issue_channel_id column

Revision ID: d1c4b7e93a25
Revises: a7d3f19c60be
Create Date: 2026-08-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1c4b7e93a25"
down_revision: str | None = "a7d3f19c60be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("issue_channel_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "issue_channel_id")
