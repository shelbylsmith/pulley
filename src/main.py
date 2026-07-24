import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.config import settings
from src.db.migrate import upgrade_to_head
from src.routers import auth, github_webhooks, health, internal, slack_commands, slack_events
from src.scheduler import scheduler_loop

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pulley starting up")
    # Before serving: a failed migration must fail startup, not surface later
    # as per-request errors against a half-migrated schema.
    await upgrade_to_head()
    logger.info("Database schema is at head")
    scheduler_task: asyncio.Task | None = None
    if settings.scheduler_enabled:
        if settings.internal_api_token:
            logger.warning(
                "SCHEDULER_ENABLED and INTERNAL_API_TOKEN are both set; the in-app "
                "scheduler and external cron may both fire jobs. The run guard absorbs "
                "the duplicate, but this is usually operator misconfiguration."
            )
        scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    if scheduler_task is not None:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
    logger.info("Pulley shutting down")


app = FastAPI(title="Pulley", description="GitHub ↔ Slack PR integration", lifespan=lifespan)

app.include_router(health.router)
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(github_webhooks.router, prefix="/webhooks/github", tags=["github"])
app.include_router(slack_events.router, prefix="/slack/events", tags=["slack"])
app.include_router(slack_commands.router, prefix="/slack/commands", tags=["slack"])
app.include_router(internal.router, tags=["internal"])
