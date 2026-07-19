from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import slack_sdk.errors

from src.services import channel_manager
from src.services.channel_manager import _make_channel_name, _should_skip_channel


def test_make_channel_name():
    assert _make_channel_name("org/my-repo", 42) == "_pr-my-repo-42"


def test_make_channel_name_truncates():
    long_name = "org/" + "a" * 100
    name = _make_channel_name(long_name, 1)
    assert len(name) <= 80


def test_make_channel_name_sanitizes():
    # `.` gets replaced; `_` is now permitted in the sanitize regex
    name = _make_channel_name("org/My_Repo.Name", 7)
    assert name == "_pr-my_repo-name-7"


def test_should_skip_channel_marker_in_body():
    payload = {
        "pull_request": {
            "body": "This PR has _noslackchannel in description",
            "labels": [],
        }
    }
    assert _should_skip_channel(payload) is True


def test_should_skip_channel_label():
    payload = {
        "pull_request": {
            "body": "normal description",
            "labels": [{"name": "_noslackchannel"}],
        }
    }
    assert _should_skip_channel(payload) is True


def test_should_not_skip_channel():
    payload = {
        "pull_request": {
            "body": "normal PR",
            "labels": [{"name": "enhancement"}],
        }
    }
    assert _should_skip_channel(payload) is False


_ORG = SimpleNamespace(slack_bot_token="xoxb")
_DB_PR = SimpleNamespace(
    organization_id=1,
    slack_channel_id="C123",
    title_bookmark_id="Bk456",
    github_pr_number=42,
    repo_full_name="org/repo",
    html_url="https://gh/pr/42",
)


def _edited_payload(changes: dict, title: str = "New title") -> dict:
    return {
        "pull_request": {
            "id": 999,
            "number": 42,
            "title": title,
            "html_url": "https://gh/pr/42",
        },
        "repository": {"full_name": "org/repo"},
        "changes": changes,
    }


def _rename_patches():
    return [
        patch.object(
            channel_manager, "get_pr_by_repo_and_number", new=AsyncMock(return_value=_DB_PR)
        ),
        patch.object(channel_manager, "get_org", new=AsyncMock(return_value=_ORG)),
        patch.object(channel_manager, "set_pr_title", new=AsyncMock()),
        patch.object(channel_manager, "set_pr_base_branch", new=AsyncMock()),
        patch.object(channel_manager.pr_digest_service, "update", new=AsyncMock()),
        patch.object(channel_manager.slack_service, "post_message", new=AsyncMock()),
        patch.object(channel_manager.slack_service, "set_channel_topic", new=AsyncMock()),
        patch.object(channel_manager.slack_service, "edit_bookmark", new=AsyncMock()),
    ]


async def test_rename_propagates_new_title():
    """A title change persists the new title and refreshes every Slack surface."""
    for p in _rename_patches():
        p.start()
    try:
        await channel_manager.handle_pr_updated(
            _edited_payload({"title": {"from": "Old title"}}), "edited"
        )

        channel_manager.set_pr_title.assert_awaited_once_with(999, "New title")
        channel_manager.slack_service.set_channel_topic.assert_awaited_once()
        channel_manager.slack_service.edit_bookmark.assert_awaited_once()
        assert (
            channel_manager.slack_service.edit_bookmark.call_args.kwargs["title"] == "#42 New title"
        )
        channel_manager.pr_digest_service.update.assert_awaited_once()
        post_text = channel_manager.slack_service.post_message.call_args.args[1]
        assert "New title" in post_text
    finally:
        patch.stopall()


async def test_body_only_edit_is_a_noop():
    """`edited` without a title change must not claim a rename or touch the title."""
    for p in _rename_patches():
        p.start()
    try:
        await channel_manager.handle_pr_updated(
            _edited_payload({"body": {"from": "old body"}}), "edited"
        )

        channel_manager.set_pr_title.assert_not_awaited()
        channel_manager.slack_service.post_message.assert_not_awaited()
        channel_manager.slack_service.set_channel_topic.assert_not_awaited()
        channel_manager.slack_service.edit_bookmark.assert_not_awaited()
        channel_manager.pr_digest_service.update.assert_not_awaited()
    finally:
        patch.stopall()


async def test_base_change_propagates_new_merge_target():
    """A base-branch change persists the new base and refreshes the digest."""
    payload = _edited_payload({"base": {"ref": {"from": "main"}}})
    payload["pull_request"]["base"] = {"ref": "release/2.0"}
    for p in _rename_patches():
        p.start()
    try:
        await channel_manager.handle_pr_updated(payload, "edited")

        channel_manager.set_pr_base_branch.assert_awaited_once_with(999, "release/2.0")
        channel_manager.set_pr_title.assert_not_awaited()
        channel_manager.pr_digest_service.update.assert_awaited_once()
        post_text = channel_manager.slack_service.post_message.call_args.args[1]
        assert "main" in post_text and "release/2.0" in post_text
    finally:
        patch.stopall()


# ── handle_pr_opened idempotency ──────────────────────────

_OPEN_ORG = SimpleNamespace(id=1, slack_bot_token="xoxb", github_org_login="org")


def _opened_payload() -> dict:
    return {
        "pull_request": {
            "id": 999,
            "number": 42,
            "title": "Add things",
            "html_url": "https://gh/pr/42",
            "draft": False,
            "user": {"login": "alice", "id": 7},
            "base": {"ref": "main"},
            "head": {"ref": "feat/x"},
            "requested_reviewers": [],
            "labels": [],
            "body": "normal",
        },
        "repository": {"full_name": "org/repo", "owner": {"login": "org", "id": 1}},
        "installation": {"id": 555},
    }


def _open_patches() -> list:
    """Patch every external call handle_pr_opened makes *except* the three that
    differ per test: get_pr_by_github_id, create_channel, find_channel_by_name.
    """
    return [
        patch.object(
            channel_manager, "_get_or_backfill_org", new=AsyncMock(return_value=_OPEN_ORG)
        ),
        patch.object(
            channel_manager, "get_slack_ids_for_github_usernames", new=AsyncMock(return_value=[])
        ),
        patch.object(channel_manager, "create_pr_and_channel", new=AsyncMock(return_value=_DB_PR)),
        patch.object(channel_manager, "set_pr_reviewers", new=AsyncMock(return_value=_DB_PR)),
        patch.object(channel_manager.pr_digest_service, "post_initial", new=AsyncMock()),
        patch.object(channel_manager.slack_service, "set_channel_topic", new=AsyncMock()),
        patch.object(
            channel_manager.slack_service, "add_bookmark", new=AsyncMock(return_value={"id": "Bk1"})
        ),
        patch.object(channel_manager.slack_service, "post_message", new=AsyncMock()),
        patch.object(channel_manager.slack_service, "invite_to_channel", new=AsyncMock()),
    ]


async def test_opened_skips_when_already_tracked():
    """A redelivered `opened` for a tracked PR creates no second channel or row."""
    patches = _open_patches()
    patches.append(
        patch.object(channel_manager, "get_pr_by_github_id", new=AsyncMock(return_value=_DB_PR))
    )
    patches.append(patch.object(channel_manager.slack_service, "create_channel", new=AsyncMock()))
    for p in patches:
        p.start()
    try:
        await channel_manager.handle_pr_opened(_opened_payload())

        channel_manager.slack_service.create_channel.assert_not_awaited()
        channel_manager.create_pr_and_channel.assert_not_awaited()
    finally:
        patch.stopall()


async def test_opened_resumes_on_name_taken():
    """If the channel already exists from a crashed prior delivery, resume on it
    rather than failing the redelivery — and persist the PR against that channel.
    """
    name_taken = slack_sdk.errors.SlackApiError("name_taken", {"ok": False, "error": "name_taken"})
    patches = _open_patches()
    patches.append(
        patch.object(channel_manager, "get_pr_by_github_id", new=AsyncMock(return_value=None))
    )
    patches.append(
        patch.object(
            channel_manager.slack_service, "create_channel", new=AsyncMock(side_effect=name_taken)
        )
    )
    patches.append(
        patch.object(
            channel_manager.slack_service,
            "find_channel_by_name",
            new=AsyncMock(return_value={"id": "C_EXISTING"}),
        )
    )
    for p in patches:
        p.start()
    try:
        await channel_manager.handle_pr_opened(_opened_payload())

        channel_manager.slack_service.find_channel_by_name.assert_awaited_once()
        channel_manager.create_pr_and_channel.assert_awaited_once()
        assert (
            channel_manager.create_pr_and_channel.call_args.kwargs["slack_channel_id"]
            == "C_EXISTING"
        )
    finally:
        patch.stopall()
