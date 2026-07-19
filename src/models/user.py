from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id"), index=True
    )

    # GitHub identity
    github_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    github_username: Mapped[str] = mapped_column(String(255))
    github_access_token: Mapped[str | None] = mapped_column(Text)
    github_refresh_token: Mapped[str | None] = mapped_column(Text)
    github_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Slack identity
    slack_user_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    slack_user_name: Mapped[str | None] = mapped_column(String(255))
    slack_user_token: Mapped[str | None] = mapped_column(Text)

    is_onboarded: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped["Organization"] = relationship(back_populates="users")  # noqa: F821
    review_time_slots: Mapped[list["ReviewTimeSlot"]] = relationship(  # noqa: F821
        back_populates="user"
    )
