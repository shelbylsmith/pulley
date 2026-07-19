from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.main import app
from src.routers import internal


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def stub_jobs(monkeypatch):
    """Mock the org lookups, run guard, and per-org services so a 200 path does
    no real DB/Slack/GitHub work."""
    org = SimpleNamespace(id=7)
    recap = AsyncMock()
    stale = AsyncMock()
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(internal, "get_all_orgs_with_recap", AsyncMock(return_value=[org]))
    monkeypatch.setattr(internal, "get_all_orgs", AsyncMock(return_value=[org]))
    monkeypatch.setattr(internal, "claim_scheduled_run", claim)
    monkeypatch.setattr(internal, "run_recap_for_org", recap)
    monkeypatch.setattr(internal, "run_stale_reminders_for_org", stale)
    return SimpleNamespace(org=org, recap=recap, stale=stale, claim=claim)


@pytest.mark.parametrize("path", ["/internal/recap", "/internal/stale-reminders"])
async def test_token_unset_returns_404(client, monkeypatch, path):
    monkeypatch.setattr(settings, "internal_api_token", "")
    resp = await client.post(path)
    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["/internal/recap", "/internal/stale-reminders"])
async def test_missing_header_returns_401(client, monkeypatch, path):
    monkeypatch.setattr(settings, "internal_api_token", "s3cret")
    resp = await client.post(path)
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/internal/recap", "/internal/stale-reminders"])
async def test_wrong_token_returns_401(client, monkeypatch, path):
    monkeypatch.setattr(settings, "internal_api_token", "s3cret")
    resp = await client.post(path, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


async def test_correct_bearer_runs_recap(client, monkeypatch, stub_jobs):
    monkeypatch.setattr(settings, "internal_api_token", "s3cret")
    resp = await client.post("/internal/recap", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    stub_jobs.recap.assert_awaited_once_with(stub_jobs.org)


async def test_correct_bearer_runs_stale(client, monkeypatch, stub_jobs):
    monkeypatch.setattr(settings, "internal_api_token", "s3cret")
    resp = await client.post(
        "/internal/stale-reminders", headers={"Authorization": "Bearer s3cret"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    stub_jobs.stale.assert_awaited_once_with(stub_jobs.org)


async def test_claim_failure_skips_org(client, monkeypatch, stub_jobs):
    monkeypatch.setattr(settings, "internal_api_token", "s3cret")
    stub_jobs.claim.return_value = False
    resp = await client.post("/internal/recap", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    stub_jobs.recap.assert_not_called()
