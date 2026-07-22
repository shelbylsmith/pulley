from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.database import Base


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id"), index=True
    )

    # GitHub PR identity
    github_pr_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    github_pr_number: Mapped[int] = mapped_column(BigInteger)
    repo_full_name: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="open")  # open, closed, merged
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    head_branch: Mapped[str] = mapped_column(String(255))
    base_branch: Mapped[str] = mapped_column(String(255))
    html_url: Mapped[str] = mapped_column(Text)

    # Author
    author_github_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_github_username: Mapped[str] = mapped_column(String(255))

    # Slack channel
    slack_channel_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    no_slack_channel: Mapped[bool] = mapped_column(Boolean, default=False)

    # Digest message in the org-level PR channel (one-message-per-PR view)
    pr_digest_ts: Mapped[str | None] = mapped_column(String(64))
    # Bookmark in the PR's Slack channel showing rolled-up CI state
    ci_bookmark_id: Mapped[str | None] = mapped_column(String(64))
    # Bookmark in the PR's Slack channel linking to the PR; its title is kept in
    # sync when the PR is renamed.
    title_bookmark_id: Mapped[str | None] = mapped_column(String(64))
    # passed | failed | None — last completed CI state we posted a recap for.
    # Used to dedupe recap messages: only post on transitions, not every completion.
    last_ci_state: Mapped[str | None] = mapped_column(String(16))
    # approved | changes_requested | commented | None
    last_review_state: Mapped[str | None] = mapped_column(String(32))
    # Comma-separated GitHub usernames of currently-requested reviewers.
    reviewers: Mapped[str | None] = mapped_column(Text)
    # When the current pending review request was opened — set when the PR
    # gains its first pending reviewer, kept as more are added, cleared when
    # none remain (everyone submitted or was removed).
    review_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped["Organization"] = relationship(back_populates="pull_requests")  # noqa: F821
    slack_channel: Mapped["SlackChannel | None"] = relationship(  # noqa: F821
        back_populates="pull_request"
    )
