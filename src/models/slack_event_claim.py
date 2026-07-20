from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.database import Base


class SlackEventClaim(Base):
    """Dedupe guard for Slack event deliveries.

    Slack redelivers an event when it isn't acked within 3 seconds, so the same
    event_id can arrive several times (and concurrently with the original still
    in flight). Each delivery races to insert its event_id; only the winner
    processes. On processing failure the claim is released so a later retry can
    rescue the event instead of being dropped.

    Rows are never pruned: one small row per Slack message is negligible growth.
    """

    __tablename__ = "slack_event_claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
