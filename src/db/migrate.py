"""Programmatic `alembic upgrade head`, run from app startup.

Lives in the app (not the container entrypoint) so every deployment shape —
Docker, systemd, bare uvicorn, PaaS — applies migrations before serving.
Concurrent starts (multiple instances or workers) serialize on the advisory
lock taken in migrations/env.py; followers find an up-to-date schema and no-op.
"""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _upgrade_sync() -> None:
    # A file-less Config keeps env.py from re-running fileConfig (which would
    # clobber the app's logging) and avoids any cwd/alembic.ini dependence.
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    command.upgrade(cfg, "head")


async def upgrade_to_head() -> None:
    # env.py drives its own event loop via asyncio.run, so it must run in a
    # thread that doesn't already have one.
    await asyncio.to_thread(_upgrade_sync)
