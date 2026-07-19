"""Opt-in in-app scheduler for recap and stale-reminder jobs.

Enabled via SCHEDULER_ENABLED and started from the app lifespan. It assumes an
always-on instance: it sleeps to each minute boundary and evaluates every org's
cron for that UTC minute. Duplicate firing across multiple instances is absorbed
by the scheduler_runs claim (at-most-once per job per minute), so running more
than one instance is safe but wasteful.

Serverless or scale-to-zero deployments should leave this off and instead point
an external cron at the token-protected /internal/* endpoints.
"""

import asyncio
import logging
from datetime import UTC, datetime

from croniter import croniter

from src.config import settings
from src.db.queries import claim_scheduled_run, get_all_orgs, get_all_orgs_with_recap
from src.services.notification_service import run_recap_for_org, run_stale_reminders_for_org

logger = logging.getLogger(__name__)


async def _run_tick(now: datetime) -> None:
    """Evaluate every org's schedule for the given UTC minute and fire due jobs."""
    for org in await get_all_orgs_with_recap():
        if croniter.match(org.recap_cron or settings.recap_cron, now) and await claim_scheduled_run(
            f"recap:{org.id}", now
        ):
            await run_recap_for_org(org)

    if croniter.match(settings.stale_reminder_cron, now):
        for org in await get_all_orgs():
            if await claim_scheduled_run(f"stale:{org.id}", now):
                await run_stale_reminders_for_org(org)


async def scheduler_loop() -> None:
    logger.info("In-app scheduler started")
    while True:
        now = datetime.now(UTC)
        sleep_seconds = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(sleep_seconds)

        minute = datetime.now(UTC).replace(second=0, microsecond=0)
        # One broad catch, deliberately scoped to the tick: a background loop must
        # survive transient Slack/GitHub/DB errors rather than die on one bad minute.
        try:
            await _run_tick(minute)
        except Exception:
            logger.exception("Scheduler tick failed for %s", minute.isoformat())
