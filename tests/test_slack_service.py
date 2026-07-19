from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import slack_sdk.errors
from slack_sdk.http_retry.response import HttpResponse
from slack_sdk.http_retry.state import RetryState

from src.services import slack_service

# ── find_channel_by_name ──────────────────────────────────


async def test_find_channel_by_name_paginates():
    page1 = {
        "channels": [{"name": "other", "id": "C1"}],
        "response_metadata": {"next_cursor": "abc"},
    }
    page2 = {
        "channels": [{"name": "_pr-repo-42", "id": "C2"}],
        "response_metadata": {"next_cursor": ""},
    }
    client = SimpleNamespace(conversations_list=AsyncMock(side_effect=[page1, page2]))
    with patch.object(slack_service, "_client", return_value=client):
        found = await slack_service.find_channel_by_name("_pr-repo-42", token="xoxb")

    assert found["id"] == "C2"
    assert client.conversations_list.await_count == 2


async def test_find_channel_by_name_absent_returns_none():
    page = {
        "channels": [{"name": "other", "id": "C1"}],
        "response_metadata": {"next_cursor": ""},
    }
    client = SimpleNamespace(conversations_list=AsyncMock(return_value=page))
    with patch.object(slack_service, "_client", return_value=client):
        found = await slack_service.find_channel_by_name("missing", token="xoxb")

    assert found is None


async def test_find_channel_by_name_handles_missing_metadata():
    # A single-page response may omit response_metadata entirely.
    page = {"channels": [{"name": "other", "id": "C1"}]}
    client = SimpleNamespace(conversations_list=AsyncMock(return_value=page))
    with patch.object(slack_service, "_client", return_value=client):
        found = await slack_service.find_channel_by_name("missing", token="xoxb")

    assert found is None


# ── invite_to_channel ─────────────────────────────────────


async def test_invite_swallows_already_in_channel():
    """Re-requesting a reviewer invites someone already in the channel; Slack
    returns already_in_channel, which must be treated as a no-op, not a crash."""
    err = slack_sdk.errors.SlackApiError("failed", {"error": "already_in_channel"})
    client = SimpleNamespace(conversations_invite=AsyncMock(side_effect=err))
    with patch.object(slack_service, "_client", return_value=client):
        await slack_service.invite_to_channel("C1", ["U1"], token="xoxb")
    client.conversations_invite.assert_awaited_once()


async def test_invite_reraises_other_errors():
    err = slack_sdk.errors.SlackApiError("failed", {"error": "channel_not_found"})
    client = SimpleNamespace(conversations_invite=AsyncMock(side_effect=err))
    with (
        patch.object(slack_service, "_client", return_value=client),
        pytest.raises(slack_sdk.errors.SlackApiError),
    ):
        await slack_service.invite_to_channel("C1", ["U1"], token="xoxb")


async def test_invite_noop_on_empty_user_list():
    client = SimpleNamespace(conversations_invite=AsyncMock())
    with patch.object(slack_service, "_client", return_value=client):
        await slack_service.invite_to_channel("C1", [], token="xoxb")
    client.conversations_invite.assert_not_called()


# ── _BodyRateLimitRetryHandler ────────────────────────────


def _handler() -> slack_service._BodyRateLimitRetryHandler:
    return slack_service._BodyRateLimitRetryHandler(max_retry_count=3)


async def _can_retry(response: HttpResponse) -> bool:
    return await _handler()._can_retry_async(state=RetryState(), request=None, response=response)


async def test_retry_handler_retries_on_body_too_many_requests():
    resp = HttpResponse(
        status_code=200, headers={}, body={"ok": False, "error": "too_many_requests"}
    )
    assert await _can_retry(resp) is True


async def test_retry_handler_retries_on_body_ratelimited():
    resp = HttpResponse(status_code=200, headers={}, body={"ok": False, "error": "ratelimited"})
    assert await _can_retry(resp) is True


async def test_retry_handler_still_retries_on_429():
    resp = HttpResponse(status_code=429, headers={}, body=None)
    assert await _can_retry(resp) is True


async def test_retry_handler_ignores_success():
    resp = HttpResponse(status_code=200, headers={}, body={"ok": True})
    assert await _can_retry(resp) is False


async def test_retry_handler_ignores_other_body_errors():
    resp = HttpResponse(status_code=200, headers={}, body={"ok": False, "error": "name_taken"})
    assert await _can_retry(resp) is False
