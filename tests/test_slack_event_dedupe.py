"""Dedupe of redelivered Slack events via the event_id claim."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app
from src.routers import slack_events


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def stub_signature(monkeypatch):
    monkeypatch.setattr(slack_events, "verify_slack_signature", lambda *a, **k: True)


def _post(client, event_id="Ev123"):
    return client.post(
        "/slack/events",
        json={
            "type": "event_callback",
            "event_id": event_id,
            "team_id": "T1",
            "event": {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": "1.1"},
        },
        headers={"X-Slack-Request-Timestamp": "0", "X-Slack-Signature": "v0=stub"},
    )


@pytest.mark.asyncio
async def test_claim_winner_dispatches(client, monkeypatch):
    claim = AsyncMock(return_value=True)
    dispatch = AsyncMock()
    monkeypatch.setattr(slack_events, "claim_slack_event", claim)
    monkeypatch.setattr(slack_events, "_dispatch_event", dispatch)

    resp = await _post(client)

    assert resp.status_code == 200
    claim.assert_awaited_once_with("Ev123")
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_delivery_is_skipped(client, monkeypatch):
    claim = AsyncMock(return_value=False)
    dispatch = AsyncMock()
    monkeypatch.setattr(slack_events, "claim_slack_event", claim)
    monkeypatch.setattr(slack_events, "_dispatch_event", dispatch)

    resp = await _post(client)

    assert resp.status_code == 200
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_failed_processing_releases_claim(client, monkeypatch):
    release = AsyncMock()
    monkeypatch.setattr(slack_events, "claim_slack_event", AsyncMock(return_value=True))
    monkeypatch.setattr(slack_events, "release_slack_event", release)
    monkeypatch.setattr(
        slack_events, "_dispatch_event", AsyncMock(side_effect=RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError):
        await _post(client)

    release.assert_awaited_once_with("Ev123")


@pytest.mark.asyncio
async def test_successful_processing_keeps_claim(client, monkeypatch):
    release = AsyncMock()
    monkeypatch.setattr(slack_events, "claim_slack_event", AsyncMock(return_value=True))
    monkeypatch.setattr(slack_events, "release_slack_event", release)
    monkeypatch.setattr(slack_events, "_dispatch_event", AsyncMock())

    await _post(client)

    release.assert_not_called()
