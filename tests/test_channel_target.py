from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services import channel_target
from src.services.channel_target import resolve_channel_pr

_OWNER = SimpleNamespace(id=1, github_pr_number=5, state="open", slack_thread_ts=None)
_CLOSED_OWNER = SimpleNamespace(id=1, github_pr_number=5, state="merged", slack_thread_ts=None)
_STACKED = SimpleNamespace(id=2, github_pr_number=6, state="open", slack_thread_ts="200.1")
_STACKED_2 = SimpleNamespace(id=3, github_pr_number=7, state="open", slack_thread_ts="300.1")


def _patches(owner, open_prs, *, by_thread=None, mapping=None, by_id=None):
    return [
        patch.object(channel_target, "get_pr_by_channel", new=AsyncMock(return_value=owner)),
        patch.object(
            channel_target, "get_open_prs_in_channel", new=AsyncMock(return_value=open_prs)
        ),
        patch.object(
            channel_target, "get_pr_by_channel_thread", new=AsyncMock(return_value=by_thread)
        ),
        patch.object(
            channel_target, "get_message_mapping_in_channel", new=AsyncMock(return_value=mapping)
        ),
        patch.object(channel_target, "get_pr_by_id", new=AsyncMock(return_value=by_id)),
        patch.object(
            channel_target, "get_pr_in_channel_by_number", new=AsyncMock(return_value=by_id)
        ),
    ]


async def _resolve(patches, **kwargs):
    for p in patches:
        p.start()
    try:
        return await resolve_channel_pr("C1", **kwargs)
    finally:
        patch.stopall()


async def test_a_stacked_prs_parent_message_resolves_to_that_pr():
    target = await _resolve(
        _patches(_OWNER, [_OWNER, _STACKED], by_thread=_STACKED), message_ts="200.1"
    )
    assert target.pr is _STACKED


async def test_a_mapped_message_resolves_to_its_pr():
    mapping = SimpleNamespace(pull_request_id=1)
    target = await _resolve(
        _patches(_CLOSED_OWNER, [_STACKED], mapping=mapping, by_id=_CLOSED_OWNER),
        message_ts="150.1",
    )
    # A reply in a closed owner's review thread still belongs to the owner.
    assert target.pr is _CLOSED_OWNER


async def test_the_open_owner_is_the_channels_default():
    target = await _resolve(_patches(_OWNER, [_OWNER, _STACKED]))
    assert target.pr is _OWNER


async def test_a_closed_owner_hands_the_channel_to_its_only_open_pr():
    target = await _resolve(_patches(_CLOSED_OWNER, [_STACKED]))
    assert target.pr is _STACKED


async def test_a_closed_owner_with_several_open_prs_is_ambiguous():
    target = await _resolve(_patches(_CLOSED_OWNER, [_STACKED, _STACKED_2]))
    assert target.pr is None
    assert target.ambiguous


async def test_strict_resolution_will_not_guess_between_open_prs():
    target = await _resolve(_patches(_OWNER, [_OWNER, _STACKED]), strict=True)
    assert target.ambiguous


async def test_a_pr_number_picks_that_pr():
    target = await _resolve(
        _patches(_OWNER, [_OWNER, _STACKED], by_id=_STACKED), pr_number=6, strict=True
    )
    assert target.pr is _STACKED


async def test_a_fully_closed_channel_is_about_its_owner():
    target = await _resolve(_patches(_CLOSED_OWNER, []))
    assert target.pr is _CLOSED_OWNER
    assert not target.ambiguous
