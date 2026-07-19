"""add title_bookmark_id column

Revision ID: d5c8a1b9e2f4
Revises: c7e1a2b3d4f5
Create Date: 2026-06-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5c8a1b9e2f4"
down_revision: str | None = "c7e1a2b3d4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pull_requests", sa.Column("title_bookmark_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("pull_requests", "title_bookmark_id")
