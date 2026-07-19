"""add slack_ts_extra column to message_mappings

Revision ID: f2a9c6d4e8b1
Revises: d5c8a1b9e2f4
Create Date: 2026-06-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a9c6d4e8b1"
down_revision: str | None = "d5c8a1b9e2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_mappings", sa.Column("slack_ts_extra", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("message_mappings", "slack_ts_extra")
