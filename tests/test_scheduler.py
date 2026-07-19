from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import scheduler
from src.config import settings

# 2026-07-20 is a Monday; the default crons target weekdays (1-5).
_MON_0900 = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
_MON_1000 = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
_MON_1400 = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


@pytest.fixture
def stubbed(monkeypatch):
    recap = AsyncMock()
    stale = AsyncMock()
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduler, "claim_scheduled_run", claim)
    monkeypatch.setattr(scheduler, "run_recap_for_org", recap)
    monkeypatch.setattr(scheduler, "run_stale_reminders_for_org", stale)
    # Default: no orgs, individual tests override as needed.
    monkeypatch.setattr(scheduler, "get_all_orgs_with_recap", AsyncMock(return_value=[]))
    monkeypatch.setattr(scheduler, "get_all_orgs", AsyncMock(return_value=[]))
    return SimpleNamespace(recap=recap, stale=stale, claim=claim, monkeypatch=monkeypatch)


async def test_matching_recap_cron_fires_once(stubbed):
    org = SimpleNamespace(id=1, recap_cron="0 9 * * 1-5")
    stubbed.monkeypatch.setattr(scheduler, "get_all_orgs_with_recap", AsyncMock(return_value=[org]))
    await scheduler._run_tick(_MON_0900)
    stubbed.recap.assert_awaited_once_with(org)


async def test_non_matching_recap_cron_does_not_fire(stubbed):
    org = SimpleNamespace(id=1, recap_cron="0 9 * * 1-5")
    stubbed.monkeypatch.setattr(scheduler, "get_all_orgs_with_recap", AsyncMock(return_value=[org]))
    await scheduler._run_tick(_MON_1000)
    stubbed.recap.assert_not_called()


async def test_null_recap_cron_falls_back_to_settings(stubbed):
    stubbed.monkeypatch.setattr(settings, "recap_cron", "0 10 * * 1-5")
    org = SimpleNamespace(id=1, recap_cron=None)
    stubbed.monkeypatch.setattr(scheduler, "get_all_orgs_with_recap", AsyncMock(return_value=[org]))
    await scheduler._run_tick(_MON_1000)
    stubbed.recap.assert_awaited_once_with(org)


async def test_failed_claim_skips_recap(stubbed):
    org = SimpleNamespace(id=1, recap_cron="0 9 * * 1-5")
    stubbed.monkeypatch.setattr(scheduler, "get_all_orgs_with_recap", AsyncMock(return_value=[org]))
    stubbed.claim.return_value = False
    await scheduler._run_tick(_MON_0900)
    stubbed.recap.assert_not_called()


async def test_matching_stale_cron_fires_per_org(stubbed):
    orgs = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    stubbed.monkeypatch.setattr(scheduler, "get_all_orgs", AsyncMock(return_value=orgs))
    await scheduler._run_tick(_MON_1400)
    assert stubbed.stale.await_count == 2
    stubbed.stale.assert_any_await(orgs[0])
    stubbed.stale.assert_any_await(orgs[1])


async def test_non_matching_stale_cron_does_not_fire(stubbed):
    get_all = AsyncMock(return_value=[SimpleNamespace(id=1)])
    stubbed.monkeypatch.setattr(scheduler, "get_all_orgs", get_all)
    await scheduler._run_tick(_MON_0900)
    stubbed.stale.assert_not_called()
    get_all.assert_not_called()
