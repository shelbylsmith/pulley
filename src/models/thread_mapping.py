from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.database import Base


class ThreadMapping(Base):
    """Maps a GitHub review thread to a Slack thread (parent message timestamp)."""

    __tablename__ = "thread_mappings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(BigInteger, index=True)
    github_thread_id: Mapped[str] = mapped_column(String(255), index=True)
    slack_channel_id: Mapped[str] = mapped_column(String(64))
    slack_thread_ts: Mapped[str] = mapped_column(String(64))
    file_path: Mapped[str] = mapped_column(Text, default="")
