"""add reviewers column

Revision ID: e94bfda656d3
Revises: 0dd0fda55a85
Create Date: 2026-04-24 14:59:02.488051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e94bfda656d3"
down_revision: str | None = "0dd0fda55a85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pull_requests", sa.Column("reviewers", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("pull_requests", "reviewers")
