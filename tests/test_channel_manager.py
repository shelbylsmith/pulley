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
    slack_thread_ts=None,
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


# ── Channel label gate ────────────────────────────────────


async def test_opened_waits_for_label_when_gate_configured():
    """With a channel label configured, `opened` never opens a channel itself."""
    with (
        patch.object(channel_manager.settings, "slack_channel_label", "human-review"),
        patch.object(channel_manager, "_open_channel", new=AsyncMock()) as open_channel,
    ):
        await channel_manager.handle_pr_opened(_opened_payload())
    open_channel.assert_not_awaited()


async def test_opened_opens_channel_without_gate():
    with patch.object(channel_manager, "_open_channel", new=AsyncMock()) as open_channel:
        await channel_manager.handle_pr_opened(_opened_payload())
    open_channel.assert_awaited_once()


def _labeled_payload(label: str, *, on_pr: bool = True, state: str = "open") -> dict:
    payload = _opened_payload()
    payload["label"] = {"name": label}
    payload["pull_request"]["state"] = state
    if on_pr:
        payload["pull_request"]["labels"] = [{"name": label}]
    return payload


async def test_labeled_with_channel_label_opens_channel():
    with (
        patch.object(channel_manager.settings, "slack_channel_label", "human-review"),
        patch.object(channel_manager, "_open_channel", new=AsyncMock()) as open_channel,
    ):
        await channel_manager.handle_pr_labeled(_labeled_payload("Human-Review"))
    open_channel.assert_awaited_once()


async def test_labeled_with_other_label_is_ignored():
    with (
        patch.object(channel_manager.settings, "slack_channel_label", "human-review"),
        patch.object(channel_manager, "_open_channel", new=AsyncMock()) as open_channel,
    ):
        await channel_manager.handle_pr_labeled(_labeled_payload("claude-review"))
    open_channel.assert_not_awaited()


async def test_labeled_on_closed_pr_is_ignored():
    with (
        patch.object(channel_manager.settings, "slack_channel_label", "human-review"),
        patch.object(channel_manager, "_open_channel", new=AsyncMock()) as open_channel,
    ):
        await channel_manager.handle_pr_labeled(_labeled_payload("human-review", state="closed"))
    open_channel.assert_not_awaited()


async def test_labeled_without_gate_is_ignored():
    """Without a configured label, `opened` already opened the channel."""
    with patch.object(channel_manager, "_open_channel", new=AsyncMock()) as open_channel:
        await channel_manager.handle_pr_labeled(_labeled_payload("human-review"))
    open_channel.assert_not_awaited()


async def test_reopened_untracked_pr_without_label_gets_no_channel():
    patches = _open_patches()
    patches.append(
        patch.object(channel_manager, "get_pr_by_github_id", new=AsyncMock(return_value=None))
    )
    patches.append(patch.object(channel_manager.slack_service, "create_channel", new=AsyncMock()))
    patches.append(patch.object(channel_manager.settings, "slack_channel_label", "human-review"))
    for p in patches:
        p.start()
    try:
        await channel_manager.handle_pr_reopened(_opened_payload())
        channel_manager.slack_service.create_channel.assert_not_awaited()
    finally:
        patch.stopall()


# ── Stack channels ────────────────────────────────────────

_PARENT = SimpleNamespace(slack_channel_id="C_STACK", github_pr_number=41)


def _stack_patches(parent) -> list:
    patches = _open_patches()
    patches += [
        patch.object(channel_manager.settings, "stack_channels", True),
        patch.object(channel_manager, "get_pr_by_github_id", new=AsyncMock(return_value=None)),
        patch.object(channel_manager, "get_stack_parent", new=AsyncMock(return_value=parent)),
        patch.object(
            channel_manager.slack_service,
            "create_channel",
            new=AsyncMock(return_value={"id": "C_NEW"}),
        ),
        patch.object(channel_manager.slack_service, "unarchive_channel", new=AsyncMock()),
    ]
    # The parent message's ts becomes the stacked PR's thread.
    patches.append(
        patch.object(
            channel_manager.slack_service,
            "post_message",
            new=AsyncMock(return_value={"ts": "200.1"}),
        )
    )
    return patches


async def test_stacked_pr_joins_its_parents_channel():
    payload = _opened_payload()
    payload["pull_request"]["base"] = {"ref": "feat/parent"}
    for p in _stack_patches(_PARENT):
        p.start()
    try:
        await channel_manager.handle_pr_opened(payload)

        channel_manager.get_stack_parent.assert_awaited_once_with("org/repo", "feat/parent")
        channel_manager.slack_service.create_channel.assert_not_awaited()
        channel_manager.slack_service.set_channel_topic.assert_not_awaited()
        channel_manager.slack_service.add_bookmark.assert_not_awaited()
        post = channel_manager.slack_service.post_message.await_args
        assert post.args[0] == "C_STACK"
        assert post.kwargs.get("thread_ts") is None  # the parent message is top-level
        assert "stacked on #41" in post.args[1]
        created = channel_manager.create_pr_and_channel.await_args.kwargs
        assert created["slack_channel_id"] == "C_STACK"
        assert created["slack_thread_ts"] == "200.1"
        channel_manager.pr_digest_service.post_initial.assert_awaited_once()
    finally:
        patch.stopall()


async def test_pr_without_a_tracked_parent_gets_its_own_channel():
    for p in _stack_patches(None):
        p.start()
    try:
        await channel_manager.handle_pr_opened(_opened_payload())
        channel_manager.slack_service.create_channel.assert_awaited_once()
        assert "slack_thread_ts" not in channel_manager.create_pr_and_channel.await_args.kwargs
    finally:
        patch.stopall()


async def test_stack_lookup_is_off_by_default():
    patches = _stack_patches(_PARENT)
    patches[len(_open_patches())] = patch.object(channel_manager.settings, "stack_channels", False)
    for p in patches:
        p.start()
    try:
        await channel_manager.handle_pr_opened(_opened_payload())
        channel_manager.get_stack_parent.assert_not_awaited()
        channel_manager.slack_service.create_channel.assert_awaited_once()
    finally:
        patch.stopall()


_STACKED = SimpleNamespace(
    organization_id=1,
    slack_channel_id="C_STACK",
    slack_thread_ts="200.1",
    title_bookmark_id=None,
    github_pr_number=42,
    repo_full_name="org/repo",
    html_url="https://gh/pr/42",
)


def _closed_payload(merged: bool = True) -> dict:
    return {
        "pull_request": {"id": 999, "number": 42, "merged": merged},
        "repository": {"full_name": "org/repo"},
    }


async def _close(db_pr, remaining: int):
    with (
        patch.object(channel_manager, "close_pr", new=AsyncMock(return_value=db_pr)),
        patch.object(channel_manager, "get_org", new=AsyncMock(return_value=_ORG)),
        patch.object(
            channel_manager, "count_open_prs_in_channel", new=AsyncMock(return_value=remaining)
        ),
        patch.object(channel_manager.pr_digest_service, "update", new=AsyncMock()),
        patch.object(channel_manager.slack_service, "post_message", new=AsyncMock()) as post,
        patch.object(channel_manager.slack_service, "archive_channel", new=AsyncMock()) as archive,
    ):
        await channel_manager.handle_pr_closed(_closed_payload())
    return post, archive


async def test_closing_a_pr_keeps_the_channel_while_its_stack_is_open():
    post, archive = await _close(_STACKED, remaining=2)
    archive.assert_not_awaited()
    assert post.await_args.kwargs["thread_ts"] == "200.1"


async def test_closing_the_last_open_pr_archives_the_channel():
    post, archive = await _close(_DB_PR, remaining=0)
    archive.assert_awaited_once_with("C123", token="xoxb")
    assert post.await_args.kwargs["thread_ts"] is None


async def test_stacked_pr_push_notice_goes_in_its_thread():
    payload = {
        "pull_request": {"id": 999, "number": 42, "head": {"ref": "feat/x"}},
        "repository": {"full_name": "org/repo"},
    }
    with (
        patch.object(
            channel_manager, "get_pr_by_repo_and_number", new=AsyncMock(return_value=_STACKED)
        ),
        patch.object(channel_manager, "get_org", new=AsyncMock(return_value=_ORG)),
        patch.object(channel_manager.slack_service, "post_message", new=AsyncMock()) as post,
    ):
        await channel_manager.handle_pr_updated(payload, "synchronize")
    assert post.await_args.args[0] == "C_STACK"
    assert post.await_args.kwargs["thread_ts"] == "200.1"


async def test_renaming_a_stacked_pr_leaves_the_channel_topic_alone():
    patches = _rename_patches()
    patches[0] = patch.object(
        channel_manager, "get_pr_by_repo_and_number", new=AsyncMock(return_value=_STACKED)
    )
    for p in patches:
        p.start()
    try:
        await channel_manager.handle_pr_updated(
            _edited_payload({"title": {"from": "Old title"}}), "edited"
        )
        channel_manager.set_pr_title.assert_awaited_once_with(999, "New title")
        channel_manager.slack_service.set_channel_topic.assert_not_awaited()
        channel_manager.slack_service.edit_bookmark.assert_not_awaited()
        assert channel_manager.slack_service.post_message.await_args.kwargs["thread_ts"] == "200.1"
    finally:
        patch.stopall()
