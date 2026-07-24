from unittest.mock import AsyncMock, patch

from src import main


async def test_lifespan_migrates_before_serving():
    """Migrations must be applied (and must succeed) before the app yields to
    serving — regardless of deployment shape, since this is app-level."""
    with (
        patch.object(main, "upgrade_to_head", new=AsyncMock()) as upgrade,
        patch.object(main.settings, "scheduler_enabled", False),
    ):
        async with main.lifespan(main.app):
            upgrade.assert_awaited_once()


async def test_lifespan_migration_failure_aborts_startup():
    with (
        patch.object(main, "upgrade_to_head", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(main.settings, "scheduler_enabled", False),
    ):
        try:
            async with main.lifespan(main.app):
                raise AssertionError("lifespan should not have yielded")
        except RuntimeError as e:
            assert str(e) == "boom"
