"""Slack Events API endpoint — handles URL verification and event dispatch."""

import logging

from fastapi import APIRouter, HTTPException, Request

from src.config import settings
from src.utils.webhook_verify import verify_slack_signature

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("")
async def slack_event(request: Request):
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(body, timestamp, signature, settings.slack_signing_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()

    # URL verification challenge
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    if payload.get("type") == "event_callback":
        event = payload["event"]
        await _dispatch_event(event, payload.get("team_id"))

    return {"ok": True}


async def _dispatch_event(event: dict, team_id: str | None) -> None:
    event_type = event.get("type")

    if event_type == "message":
        await _handle_message_event(event, team_id)
        return

    # Ignore bot-triggered non-message events to prevent echo loops
    if event.get("bot_id"):
        return

    if event_type == "reaction_added":
        await _handle_reaction_added(event, team_id)

    elif event_type == "app_home_opened":
        await _handle_app_home_opened(event, team_id)

    else:
        logger.debug("Unhandled Slack event type=%s", event_type)


async def _handle_message_event(event: dict, team_id: str | None) -> None:
    subtype = event.get("subtype")

    if subtype is None:
        if event.get("bot_id"):
            return
        await _handle_channel_message(event, team_id)
    elif subtype == "message_changed":
        await _handle_message_changed(event, team_id)
    elif subtype == "message_deleted":
        await _handle_message_deleted(event, team_id)


async def _handle_channel_message(event: dict, team_id: str | None) -> None:
    """Forward Slack channel messages to GitHub as issue comments."""
    from src.services.sync_service import handle_slack_message

    channel_id = event["channel"]
    user_id = event.get("user")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts")
    message_ts = event["ts"]

    if not text or not user_id:
        return

    await handle_slack_message(
        channel_id=channel_id,
        slack_user_id=user_id,
        text=text,
        thread_ts=thread_ts,
        message_ts=message_ts,
        team_id=team_id,
    )


async def _handle_message_changed(event: dict, team_id: str | None) -> None:
    """Mirror an edited Slack message onto its GitHub comment."""
    from src.services.sync_service import handle_slack_message_edited

    message = event.get("message", {})
    # Only real user edits: link-unfurl and similar refreshes also fire
    # message_changed but carry no `edited` marker.
    if not message.get("edited"):
        return
    if message.get("bot_id") or message.get("subtype"):
        return

    user_id = message.get("user")
    text = message.get("text", "")
    message_ts = message.get("ts")
    if not user_id or not message_ts:
        return

    await handle_slack_message_edited(
        channel_id=event["channel"],
        slack_user_id=user_id,
        text=text,
        message_ts=message_ts,
    )


async def _handle_message_deleted(event: dict, team_id: str | None) -> None:
    """Mirror a deleted Slack message onto its GitHub comment."""
    from src.services.sync_service import handle_slack_message_deleted

    deleted_ts = event.get("deleted_ts")
    if not deleted_ts:
        return

    await handle_slack_message_deleted(
        channel_id=event["channel"],
        message_ts=deleted_ts,
    )


async def _handle_reaction_added(event: dict, team_id: str | None) -> None:
    """Handle reactions — e.g., 🔍 to self-assign as reviewer."""
    from src.services.channel_manager import handle_reaction_reviewer

    reaction = event.get("reaction")
    if reaction == "mag":  # 🔍
        await handle_reaction_reviewer(
            channel_id=event["item"]["channel"],
            slack_user_id=event["user"],
            team_id=team_id,
        )


async def _handle_app_home_opened(event: dict, team_id: str | None) -> None:
    """Render the App Home tab — onboarding or connected state."""
    from src.db.queries import get_org_by_slack_team
    from src.services.app_home_service import handle_app_home_opened

    if not team_id:
        return

    # Get the bot token from the org record, or fall back to settings
    org = await get_org_by_slack_team(team_id)
    bot_token = org.slack_bot_token if org else None

    await handle_app_home_opened(
        slack_user_id=event["user"],
        team_id=team_id,
        bot_token=bot_token or settings.slack_bot_token,
    )
