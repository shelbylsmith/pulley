"""add ci_bookmark_id column

Revision ID: 3f1a8c4b2d10
Revises: e94bfda656d3
Create Date: 2026-04-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f1a8c4b2d10"
down_revision: str | None = "e94bfda656d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pull_requests", sa.Column("ci_bookmark_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("pull_requests", "ci_bookmark_id")
