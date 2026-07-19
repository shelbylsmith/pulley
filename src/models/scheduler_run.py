from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.database import Base


class SchedulerRun(Base):
    """At-most-once guard for scheduled jobs.

    A row records that a named job for a given scheduled minute has been claimed.
    The unique constraint on (job_name, scheduled_for) lets concurrent instances
    or overlapping cron retries race to insert; only one wins, and the winner is
    the one that runs the job.
    """

    __tablename__ = "scheduler_runs"
    __table_args__ = (UniqueConstraint("job_name", "scheduled_for", name="uq_scheduler_runs_job"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(255), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
