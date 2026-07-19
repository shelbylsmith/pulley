from datetime import datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.services.timeslot_service import is_user_available


def test_no_slots_always_available():
    assert is_user_available([]) is True


def test_within_slot():
    mock_now = datetime(2026, 4, 8, 10, 30, tzinfo=ZoneInfo("UTC"))  # Wednesday = weekday 2
    with patch("src.services.timeslot_service.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now

        slots = [
            {"day_of_week": 2, "start_time": time(9, 0), "end_time": time(12, 0)},
        ]
        assert is_user_available(slots, "UTC") is True


def test_outside_slot():
    mock_now = datetime(2026, 4, 8, 14, 0, tzinfo=ZoneInfo("UTC"))  # Wednesday 14:00
    with patch("src.services.timeslot_service.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now

        slots = [
            {"day_of_week": 2, "start_time": time(9, 0), "end_time": time(12, 0)},
        ]
        assert is_user_available(slots, "UTC") is False


def test_wrong_day():
    mock_now = datetime(2026, 4, 7, 10, 30, tzinfo=ZoneInfo("UTC"))  # Tuesday = weekday 1
    with patch("src.services.timeslot_service.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now

        slots = [
            {"day_of_week": 2, "start_time": time(9, 0), "end_time": time(12, 0)},
        ]
        assert is_user_available(slots, "UTC") is False


def test_string_times():
    mock_now = datetime(2026, 4, 8, 10, 30, tzinfo=ZoneInfo("UTC"))
    with patch("src.services.timeslot_service.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now

        slots = [
            {"day_of_week": 2, "start_time": "09:00", "end_time": "12:00"},
        ]
        assert is_user_available(slots, "UTC") is True
