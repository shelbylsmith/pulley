from unittest.mock import AsyncMock, patch

from src.routers import slack_events
from src.services import sync_service


async def test_message_changed_ignored_without_edit_marker():
    """Link-unfurl refreshes fire message_changed but carry no `edited` marker."""
    event = {
        "type": "message",
        "subtype": "message_changed",
        "channel": "C1",
        "message": {"user": "U1", "text": "x", "ts": "1.1"},
    }
    with patch.object(sync_service, "handle_slack_message_edited", new=AsyncMock()) as h:
        await slack_events._dispatch_event(event, "T1")
    h.assert_not_called()


async def test_message_changed_ignored_for_bot_edit():
    event = {
        "type": "message",
        "subtype": "message_changed",
        "channel": "C1",
        "message": {"bot_id": "B1", "text": "x", "ts": "1.1", "edited": {"user": "B1"}},
    }
    with patch.object(sync_service, "handle_slack_message_edited", new=AsyncMock()) as h:
        await slack_events._dispatch_event(event, "T1")
    h.assert_not_called()


async def test_message_changed_forwards_user_edit():
    event = {
        "type": "message",
        "subtype": "message_changed",
        "channel": "C1",
        "message": {"user": "U1", "text": "new text", "ts": "1.1", "edited": {"user": "U1"}},
    }
    with patch.object(sync_service, "handle_slack_message_edited", new=AsyncMock()) as h:
        await slack_events._dispatch_event(event, "T1")
    h.assert_awaited_once_with(
        channel_id="C1", slack_user_id="U1", text="new text", message_ts="1.1"
    )


async def test_message_deleted_forwards_deleted_ts():
    event = {
        "type": "message",
        "subtype": "message_deleted",
        "channel": "C1",
        "deleted_ts": "1.1",
    }
    with patch.object(sync_service, "handle_slack_message_deleted", new=AsyncMock()) as h:
        await slack_events._dispatch_event(event, "T1")
    h.assert_awaited_once_with(channel_id="C1", message_ts="1.1")


async def test_plain_message_still_forwarded():
    event = {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": "1.1"}
    with patch.object(sync_service, "handle_slack_message", new=AsyncMock()) as h:
        await slack_events._dispatch_event(event, "T1")
    h.assert_awaited_once()
    assert h.await_args.kwargs["message_ts"] == "1.1"
