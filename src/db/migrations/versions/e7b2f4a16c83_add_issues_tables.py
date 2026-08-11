"""add issues and issue_digest_messages tables

Revision ID: e7b2f4a16c83
Revises: d1c4b7e93a25
Create Date: 2026-08-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b2f4a16c83"
down_revision: str | None = "d1c4b7e93a25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("github_issue_id", sa.BigInteger(), nullable=False),
        sa.Column("github_issue_number", sa.BigInteger(), nullable=False),
        sa.Column("repo_full_name", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("html_url", sa.Text(), nullable=False),
        sa.Column("author_github_username", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("state_reason", sa.String(length=32), nullable=True),
        sa.Column("labels", sa.Text(), nullable=True),
        sa.Column("assignees", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_issues_organization_id"), "issues", ["organization_id"], unique=False)
    op.create_index(op.f("ix_issues_github_issue_id"), "issues", ["github_issue_id"], unique=True)
    op.create_index(op.f("ix_issues_repo_full_name"), "issues", ["repo_full_name"], unique=False)

    op.create_table(
        "issue_digest_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("issue_id", sa.BigInteger(), nullable=False),
        sa.Column("slack_channel_id", sa.String(length=64), nullable=False),
        sa.Column("slack_ts", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issue_id", "slack_channel_id", name="uq_issue_digest_issue_channel"),
    )
    op.create_index(
        op.f("ix_issue_digest_messages_issue_id"),
        "issue_digest_messages",
        ["issue_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_issue_digest_messages_issue_id"), table_name="issue_digest_messages")
    op.drop_table("issue_digest_messages")
    op.drop_index(op.f("ix_issues_repo_full_name"), table_name="issues")
    op.drop_index(op.f("ix_issues_github_issue_id"), table_name="issues")
    op.drop_index(op.f("ix_issues_organization_id"), table_name="issues")
    op.drop_table("issues")
