"""add last_ci_state column

Revision ID: 8b7d4e2a9f10
Revises: 3f1a8c4b2d10
Create Date: 2026-04-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b7d4e2a9f10"
down_revision: str | None = "3f1a8c4b2d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pull_requests", sa.Column("last_ci_state", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("pull_requests", "last_ci_state")
