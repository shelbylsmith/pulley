from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.database import Base


class Issue(Base):
    """A GitHub issue Pulley has seen, mirrored so its card can be re-rendered.

    Every `issues` webhook carries the whole issue, and each one overwrites this
    row — GitHub is the source of truth, this is the latest snapshot of it.
    """

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id"), index=True
    )

    github_issue_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    github_issue_number: Mapped[int] = mapped_column(BigInteger)
    repo_full_name: Mapped[str] = mapped_column(String(255), index=True)

    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    html_url: Mapped[str] = mapped_column(Text)
    author_github_username: Mapped[str] = mapped_column(String(255))

    # open | closed
    state: Mapped[str] = mapped_column(String(32), default="open")
    # GitHub's qualifier on a closed issue: completed | not_planned | duplicate.
    # Null while open, and on issues closed before GitHub recorded a reason.
    state_reason: Mapped[str | None] = mapped_column(String(32))

    # Comma-separated, in the order GitHub reports them.
    labels: Mapped[str | None] = mapped_column(Text)
    assignees: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
