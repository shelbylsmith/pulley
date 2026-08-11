from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.database import Base


class IssueDigestMessage(Base):
    """The card for one issue in one Slack channel.

    Same shape and reasoning as `PRDigestMessage`: a Slack ts only addresses a
    message within the channel it was posted to, and an org can repoint its issue
    channel at any time, so the ts is stored together with its channel.

    Rows accumulate: an issue that has lived in two channels keeps a row for
    each, which is what makes repointing reversible.
    """

    __tablename__ = "issue_digest_messages"
    __table_args__ = (
        UniqueConstraint("issue_id", "slack_channel_id", name="uq_issue_digest_issue_channel"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("issues.id"), index=True)
    slack_channel_id: Mapped[str] = mapped_column(String(64))
    slack_ts: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
