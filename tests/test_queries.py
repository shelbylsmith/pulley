from datetime import UTC, datetime

from src.db.queries import _next_review_requested_at

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def test_clock_starts_on_first_request():
    assert _next_review_requested_at(False, ["alice"], None, NOW) == NOW


def test_clock_keeps_earliest_when_another_reviewer_added():
    assert _next_review_requested_at(True, ["alice", "bob"], EARLIER, NOW) == EARLIER


def test_clock_cancelled_when_nobody_pending():
    assert _next_review_requested_at(True, [], EARLIER, NOW) is None


def test_clock_stays_unset_without_reviewers():
    assert _next_review_requested_at(False, [], None, NOW) is None


def test_clock_survives_for_pre_migration_rows():
    # reviewers existed before the column did: stay None until backfilled.
    assert _next_review_requested_at(True, ["alice"], None, NOW) is None
