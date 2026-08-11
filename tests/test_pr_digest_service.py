from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services import pr_digest_service

_OLD_CHANNEL = "C_OLD"
_NEW_CHANNEL = "C_NEW"


def _org(pr_channel_id: str | None = _OLD_CHANNEL):
    return SimpleNamespace(id=1, pr_channel_id=pr_channel_id, slack_bot_token="xoxb")


def _pr(**overrides):
    pr = SimpleNamespace(
        id=7,
        github_pr_id=99,
        github_pr_number=42,
        repo_full_name="acme/widgets",
        title="Add widgets",
        state="open",
        is_draft=False,
        head_branch="feat",
        base_branch="main",
        html_url="https://github.com/acme/widgets/pull/42",
        author_github_username="alice",
        slack_channel_id="C_PR",
        last_review_state=None,
        reviewers="bob",
    )
    for k, v in overrides.items():
        setattr(pr, k, v)
    return pr


@pytest.mark.asyncio
async def test_update_edits_the_message_in_the_current_channel():
    with (
        patch.object(pr_digest_service, "get_pr_by_github_id", AsyncMock(return_value=_pr())),
        patch.object(pr_digest_service, "get_pr_digest_ts", AsyncMock(return_value="111.1")),
        patch.object(pr_digest_service.slack_service, "update_message", AsyncMock()) as edit,
        patch.object(pr_digest_service.slack_service, "post_message", AsyncMock()) as post,
    ):
        await pr_digest_service.update(99, _org())

    edit.assert_awaited_once()
    assert edit.await_args.args[:2] == (_OLD_CHANNEL, "111.1")
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_repointed_channel_gets_its_own_message():
    """The ts from the old channel is not addressable in the new one, so the PR
    is posted afresh rather than edited into a message_not_found."""
    with (
        patch.object(pr_digest_service, "get_pr_by_github_id", AsyncMock(return_value=_pr())),
        # No digest recorded for the new channel — only the old one has a row.
        patch.object(pr_digest_service, "get_pr_digest_ts", AsyncMock(return_value=None)),
        patch.object(pr_digest_service, "set_pr_digest_ts", AsyncMock()) as record,
        patch.object(
            pr_digest_service.slack_service,
            "post_message",
            AsyncMock(return_value={"ts": "222.2"}),
        ) as post,
        patch.object(pr_digest_service.slack_service, "update_message", AsyncMock()) as edit,
    ):
        await pr_digest_service.update(99, _org(_NEW_CHANNEL))

    edit.assert_not_awaited()
    post.assert_awaited_once()
    assert post.await_args.args[0] == _NEW_CHANNEL
    record.assert_awaited_once_with(7, _NEW_CHANNEL, "222.2")


@pytest.mark.asyncio
async def test_pointing_back_reuses_the_original_message():
    """Repoint away and back: the original message is edited in place, so the
    old channel doesn't accumulate a duplicate digest per round trip."""
    digests = {_OLD_CHANNEL: "111.1", _NEW_CHANNEL: "222.2"}

    with (
        patch.object(pr_digest_service, "get_pr_by_github_id", AsyncMock(return_value=_pr())),
        patch.object(
            pr_digest_service,
            "get_pr_digest_ts",
            AsyncMock(side_effect=lambda pr_id, channel_id: digests.get(channel_id)),
        ),
        patch.object(pr_digest_service.slack_service, "update_message", AsyncMock()) as edit,
        patch.object(pr_digest_service.slack_service, "post_message", AsyncMock()) as post,
    ):
        await pr_digest_service.update(99, _org(_OLD_CHANNEL))

    post.assert_not_awaited()
    assert edit.await_args.args[:2] == (_OLD_CHANNEL, "111.1")


@pytest.mark.asyncio
async def test_post_initial_records_ts_against_the_channel_it_posted_to():
    with (
        patch.object(
            pr_digest_service.slack_service,
            "post_message",
            AsyncMock(return_value={"ts": "333.3"}),
        ),
        patch.object(pr_digest_service, "set_pr_digest_ts", AsyncMock()) as record,
    ):
        await pr_digest_service.post_initial(_pr(), _org(_NEW_CHANNEL))

    record.assert_awaited_once_with(7, _NEW_CHANNEL, "333.3")


@pytest.mark.asyncio
async def test_draft_pr_is_not_posted_to_a_repointed_channel():
    with (
        patch.object(
            pr_digest_service, "get_pr_by_github_id", AsyncMock(return_value=_pr(is_draft=True))
        ),
        patch.object(pr_digest_service, "get_pr_digest_ts", AsyncMock(return_value=None)),
        patch.object(pr_digest_service.slack_service, "post_message", AsyncMock()) as post,
    ):
        await pr_digest_service.update(99, _org(_NEW_CHANNEL))

    post.assert_not_awaited()
