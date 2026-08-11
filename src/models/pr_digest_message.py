from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.database import Base


class PRDigestMessage(Base):
    """The digest message for one pull request in one Slack channel.

    A Slack ts only addresses a message within the channel it was posted to, and
    an org can repoint its PR digest channel at any time, so the ts is stored
    together with its channel rather than on the PR row.

    Rows accumulate: a PR that has lived in two digest channels keeps a row for
    each. That is what makes repointing reversible — moving back to a channel
    finds its existing message and updates it in place instead of posting a
    duplicate alongside the stale one.
    """

    __tablename__ = "pr_digest_messages"
    __table_args__ = (
        UniqueConstraint("pull_request_id", "slack_channel_id", name="uq_pr_digest_pr_channel"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pull_requests.id"), index=True
    )
    slack_channel_id: Mapped[str] = mapped_column(String(64))
    slack_ts: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
