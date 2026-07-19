"""Code review time slot service — controls when users receive review notifications."""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _coerce_time(val: time | str) -> time:
    return val if isinstance(val, time) else time.fromisoformat(val)


def is_user_available(
    slots: list[dict],
    user_timezone: str = "UTC",
) -> bool:
    """Check if the user is currently within any of their review time slots.

    Each slot dict has: day_of_week (0=Mon), start_time, end_time.

    Returns True if no slots are configured (always available) or if
    the current time falls within any slot.
    """
    if not slots:
        return True

    now = datetime.now(ZoneInfo(user_timezone))
    current_day = now.weekday()
    current_time = now.time()

    for slot in slots:
        if slot["day_of_week"] != current_day:
            continue
        start = _coerce_time(slot["start_time"])
        end = _coerce_time(slot["end_time"])
        if start <= current_time <= end:
            return True

    return False


def next_available_slot(
    slots: list[dict],
    user_timezone: str = "UTC",
) -> datetime | None:
    """Find the next time the user becomes available.

    Returns None if no slots are configured.
    """
    if not slots:
        return None

    now = datetime.now(ZoneInfo(user_timezone))
    current_day = now.weekday()
    current_time = now.time()

    for day_offset in range(7):
        check_day = (current_day + day_offset) % 7
        for slot in sorted(
            [s for s in slots if s["day_of_week"] == check_day],
            key=lambda s: s["start_time"],
        ):
            start = _coerce_time(slot["start_time"])
            if day_offset == 0 and start <= current_time:
                continue
            return now.replace(
                hour=start.hour,
                minute=start.minute,
                second=0,
                microsecond=0,
            ) + timedelta(days=day_offset)

    return None
