from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.database import Base


class SlackChannel(Base):
    __tablename__ = "slack_channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pull_requests.id"), unique=True, index=True
    )

    slack_channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    slack_channel_name: Mapped[str] = mapped_column(String(80))
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pull_request: Mapped["PullRequest"] = relationship(  # noqa: F821
        back_populates="slack_channel"
    )
