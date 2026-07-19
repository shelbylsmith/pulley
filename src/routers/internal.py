"""Token-protected endpoints for an external cron (e.g. Cloud Scheduler) to drive
recap and stale-reminder jobs on serverless or multi-instance deployments.

Disabled unless INTERNAL_API_TOKEN is set: when unset the endpoints return 404
(the feature isn't advertised); when set they require Authorization: Bearer
<token>. Each org's job is claimed through the scheduler_runs guard, so retries
or overlapping crons can't double-post.
"""

import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import settings
from src.db.queries import claim_scheduled_run, get_all_orgs, get_all_orgs_with_recap
from src.services.notification_service import run_recap_for_org, run_stale_reminders_for_org

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def require_internal_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    if not settings.internal_api_token:
        raise HTTPException(status_code=404)
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, settings.internal_api_token
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


router = APIRouter(dependencies=[Depends(require_internal_token)])


def _current_minute() -> datetime:
    return datetime.now(UTC).replace(second=0, microsecond=0)


@router.post("/internal/recap")
async def trigger_daily_recap():
    minute = _current_minute()
    orgs = await get_all_orgs_with_recap()
    fired = 0
    for org in orgs:
        if not await claim_scheduled_run(f"recap:{org.id}", minute):
            continue
        await run_recap_for_org(org)
        fired += 1
    logger.info("Daily recap triggered for %d of %d orgs", fired, len(orgs))
    return {"ok": True}


@router.post("/internal/stale-reminders")
async def trigger_stale_reminders():
    minute = _current_minute()
    orgs = await get_all_orgs()
    fired = 0
    for org in orgs:
        if not await claim_scheduled_run(f"stale:{org.id}", minute):
            continue
        await run_stale_reminders_for_org(org)
        fired += 1
    logger.info("Stale PR reminders triggered for %d of %d orgs", fired, len(orgs))
    return {"ok": True}
