"""Slack slash command handlers: /lgtm, /pulley."""

import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request

from src.config import settings
from src.utils.webhook_verify import verify_slack_signature

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/lgtm")
async def lgtm_command(request: Request):
    form = await _verified_form(request)

    from src.services.command_service import handle_lgtm

    return await handle_lgtm(
        channel_id=form["channel_id"],
        slack_user_id=form["user_id"],
        team_id=form["team_id"],
        comment=form.get("text", "").strip(),
    )


@router.post("/pulley")
async def pulley_command(request: Request):
    form = await _verified_form(request)

    from src.services.command_service import handle_pulley_command

    return await handle_pulley_command(
        subcommand=form.get("text", "").strip(),
        channel_id=form["channel_id"],
        slack_user_id=form["user_id"],
        team_id=form["team_id"],
    )


async def _verified_form(request: Request) -> dict[str, str]:
    # Read the raw body FIRST — signature verification needs the exact bytes
    # Slack signed. Can't use FastAPI's Form(...) params because they consume
    # the stream before we can hash it.
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(body, timestamp, signature, settings.slack_signing_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")
    return {k: v[0] for k, v in parse_qs(body.decode()).items()}
