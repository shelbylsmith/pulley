from datetime import time

from sqlalchemy import BigInteger, ForeignKey, SmallInteger, String, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.database import Base


class ReviewTimeSlot(Base):
    """Per-user availability window for receiving code review notifications.

    day_of_week: 0=Monday … 6=Sunday (ISO convention).
    """

    __tablename__ = "review_time_slots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)

    day_of_week: Mapped[int] = mapped_column(SmallInteger)  # 0-6
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")

    user: Mapped["User"] = relationship(back_populates="review_time_slots")  # noqa: F821
