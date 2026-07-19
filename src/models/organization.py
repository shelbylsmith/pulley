from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.database import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    github_org_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    github_org_login: Mapped[str] = mapped_column(String(255))
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True)

    # Slack workspace
    slack_team_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    slack_team_name: Mapped[str | None] = mapped_column(String(255))
    slack_bot_token: Mapped[str | None] = mapped_column(Text)

    # Settings
    ci_channel_id: Mapped[str | None] = mapped_column(String(64))
    pr_channel_id: Mapped[str | None] = mapped_column(String(64))
    recap_channel_id: Mapped[str | None] = mapped_column(String(64))
    recap_cron: Mapped[str | None] = mapped_column(String(64), default="0 9 * * 1-5")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    users: Mapped[list["User"]] = relationship(back_populates="organization")  # noqa: F821
    pull_requests: Mapped[list["PullRequest"]] = relationship(  # noqa: F821
        back_populates="organization"
    )
