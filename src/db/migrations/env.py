import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

# Ensure project root is on sys.path so `src` is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import all models so Base.metadata is populated
import src.models  # noqa: F401
from src.config import settings
from src.db.database import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


# App-specific constant guarding `alembic upgrade head`. Serializes concurrent
# migration runs on multi-container platforms so they queue instead of colliding.
_MIGRATION_ADVISORY_LOCK = 745531042


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Taken inside alembic's transaction so alembic keeps ownership of the
        # commit; the transaction-scoped lock releases on commit/rollback.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_ADVISORY_LOCK}
        )
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
