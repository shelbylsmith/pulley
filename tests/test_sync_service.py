from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services import sync_service
from src.services.sync_service import (
    SLACK_TEXT_LIMIT,
    SYNC_TAG,
    _render_issue_comment_message,
    _render_review_comment_message,
    _render_review_message,
)

# ── Render helpers (pure) ─────────────────────────────────


def test_render_review_message_known_state():
    msg = _render_review_message("approved", "https://gh/r", "org/repo", 5, "looks good")
    assert msg.startswith("✅ approved on <https://gh/r|org/repo#5>")
    assert "looks good" in msg


def test_render_review_message_no_body():
    msg = _render_review_message("changes_requested", "https://gh/r", "org/repo", 5, "")
    assert msg == "🔴 requested changes on <https://gh/r|org/repo#5>"


def test_render_review_comment_message_root_has_location_and_diff():
    comment = {
        "path": "src/app.py",
        "line": 12,
        "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        "html_url": "https://gh/c",
    }
    msg = _render_review_comment_message(comment, "fix this")
    assert "`src/app.py:12`" in msg
    assert "https://gh/c" in msg
    assert "+new" in msg
    assert "fix this" in msg


def test_render_issue_comment_message():
    msg = _render_issue_comment_message("https://gh/c", "org/repo", 9, "hi")
    assert msg.startswith("<https://gh/c|commented> on org/repo#9:")
    assert "hi" in msg


# ── GitHub → Slack: edit echo guards ──────────────────────


async def test_issue_comment_edited_skips_sync_tagged_body():
    """A GitHub comment that originated from Slack carries SYNC_TAG; editing it on
    GitHub must not bounce back to Slack."""
    payload = {
        "comment": {"id": 1, "body": f"hello\n\n{SYNC_TAG}", "html_url": "u"},
        "issue": {"number": 3, "pull_request": {}},
        "repository": {"full_name": "org/repo"},
    }
    with patch.object(
        sync_service, "get_message_mapping_by_github_comment", new=AsyncMock()
    ) as get_mapping:
        await sync_service.handle_issue_comment_edited(payload)
    get_mapping.assert_not_called()


async def test_issue_comment_edited_skips_slack_origin_mapping():
    payload = {
        "comment": {"id": 1, "body": "hello", "html_url": "u", "user": {"login": "octo"}},
        "issue": {"number": 3, "pull_request": {}},
        "repository": {"full_name": "org/repo"},
    }
    slack_origin = SimpleNamespace(origin="slack", slack_channel_id="C1", slack_ts="1.1")
    with (
        patch.object(
            sync_service,
            "get_message_mapping_by_github_comment",
            new=AsyncMock(return_value=slack_origin),
        ),
        patch.object(sync_service, "_update_attributed_to_github_user", new=AsyncMock()) as upd,
    ):
        await sync_service.handle_issue_comment_edited(payload)
    upd.assert_not_called()


async def test_issue_comment_edited_updates_slack_message():
    payload = {
        "comment": {"id": 7, "body": "edited body", "html_url": "u", "user": {"login": "octo"}},
        "issue": {"number": 3, "pull_request": {}},
        "repository": {"full_name": "org/repo"},
    }
    mapping = SimpleNamespace(
        id=1, origin="github", slack_channel_id="C1", slack_ts="1.1", slack_ts_extra=None
    )
    db_pr = SimpleNamespace(organization_id=10)
    org = SimpleNamespace(slack_bot_token="xoxb")
    with (
        patch.object(
            sync_service,
            "get_message_mapping_by_github_comment",
            new=AsyncMock(return_value=mapping),
        ),
        patch.object(sync_service, "get_pr_by_repo_and_number", new=AsyncMock(return_value=db_pr)),
        patch.object(sync_service, "get_org", new=AsyncMock(return_value=org)),
        patch.object(sync_service, "_github_body_to_slack", new=AsyncMock(side_effect=lambda b: b)),
        patch.object(sync_service, "_update_attributed_to_github_user", new=AsyncMock()) as upd,
        patch.object(sync_service, "set_message_mapping_extra_ts", new=AsyncMock()) as set_extra,
    ):
        await sync_service.handle_issue_comment_edited(payload)
    upd.assert_awaited_once()
    args = upd.await_args.args
    assert args[0] == "C1"  # channel
    assert args[1] == "1.1"  # parent ts
    assert "edited body" in args[2]  # message
    set_extra.assert_awaited_once_with(1, [])  # single short message → no continuations


# ── Slack → GitHub: edit/delete echo guards ───────────────


async def test_slack_edit_skips_github_origin_mapping():
    """Editing a Slack message that mirrors a GitHub comment must not push back."""
    db_pr = SimpleNamespace(id=1, organization_id=10, repo_full_name="org/repo")
    github_origin = SimpleNamespace(origin="github", github_comment_id=5, github_comment_type="x")
    with (
        patch.object(sync_service, "get_pr_by_channel", new=AsyncMock(return_value=db_pr)),
        patch.object(
            sync_service,
            "get_message_mapping_by_slack_ts",
            new=AsyncMock(return_value=github_origin),
        ),
        patch.object(sync_service, "update_issue_comment_as_user", new=AsyncMock()) as upd_user,
        patch.object(sync_service, "update_issue_comment", new=AsyncMock()) as upd_bot,
    ):
        await sync_service.handle_slack_message_edited("C1", "U1", "new text", "1.1")
    upd_user.assert_not_called()
    upd_bot.assert_not_called()


async def test_slack_edit_updates_github_comment_as_user():
    db_pr = SimpleNamespace(id=1, organization_id=10, repo_full_name="org/repo")
    mapping = SimpleNamespace(
        origin="slack", github_comment_id=42, github_comment_type="issue_comment"
    )
    org = SimpleNamespace(slack_bot_token="xoxb", github_installation_id=99)
    user = SimpleNamespace()
    with (
        patch.object(sync_service, "get_pr_by_channel", new=AsyncMock(return_value=db_pr)),
        patch.object(
            sync_service, "get_message_mapping_by_slack_ts", new=AsyncMock(return_value=mapping)
        ),
        patch.object(sync_service, "get_org", new=AsyncMock(return_value=org)),
        patch.object(
            sync_service, "_slack_text_to_github", new=AsyncMock(side_effect=lambda t, _: t)
        ),
        patch.object(sync_service, "get_user_by_slack_id", new=AsyncMock(return_value=user)),
        patch.object(sync_service, "get_valid_user_token", new=AsyncMock(return_value="ghtok")),
        patch.object(sync_service, "update_issue_comment_as_user", new=AsyncMock()) as upd_user,
    ):
        await sync_service.handle_slack_message_edited("C1", "U1", "new text", "1.1")
    upd_user.assert_awaited_once()
    token, repo, cid, body = upd_user.await_args.args
    assert token == "ghtok"
    assert repo == "org/repo"
    assert cid == 42
    assert SYNC_TAG in body


async def test_slack_delete_removes_github_comment_and_mapping():
    db_pr = SimpleNamespace(id=1, organization_id=10, repo_full_name="org/repo")
    mapping = SimpleNamespace(
        id=55,
        origin="slack",
        github_comment_id=42,
        github_comment_type="review_comment",
        slack_user_id="U1",
    )
    org = SimpleNamespace(github_installation_id=99)
    user = SimpleNamespace()
    with (
        patch.object(sync_service, "get_pr_by_channel", new=AsyncMock(return_value=db_pr)),
        patch.object(
            sync_service, "get_message_mapping_by_slack_ts", new=AsyncMock(return_value=mapping)
        ),
        patch.object(sync_service, "get_org", new=AsyncMock(return_value=org)),
        patch.object(sync_service, "get_user_by_slack_id", new=AsyncMock(return_value=user)),
        patch.object(sync_service, "get_valid_user_token", new=AsyncMock(return_value="ghtok")),
        patch.object(sync_service, "delete_review_comment_as_user", new=AsyncMock()) as del_comment,
        patch.object(sync_service, "delete_message_mapping", new=AsyncMock()) as del_mapping,
    ):
        await sync_service.handle_slack_message_deleted("C1", "1.1")
    del_comment.assert_awaited_once_with("ghtok", "org/repo", 42)
    del_mapping.assert_awaited_once_with(55)


# ── Chunked (multi-message) comment edit/delete ───────────


async def test_resync_grows_message_count():
    # An edit makes the body span more messages: the parent is updated and the
    # extra parts are posted as continuations threaded under the parent.
    with (
        patch.object(sync_service, "split_for_slack", return_value=["p1", "p2", "p3"]),
        patch.object(sync_service, "_update_attributed_to_github_user", new=AsyncMock()) as upd,
        patch.object(
            sync_service,
            "_post_attributed_to_github_user",
            new=AsyncMock(side_effect=[["n1"], ["n2"]]),
        ) as post,
        patch.object(sync_service, "_delete_attributed_to_github_user", new=AsyncMock()) as dele,
    ):
        new_extra = await sync_service._resync_attributed_to_github_user(
            "C1", "parent", [], "x" * (SLACK_TEXT_LIMIT + 1), "octo", "xoxb"
        )
    assert new_extra == ["n1", "n2"]
    upd.assert_awaited_once()  # only the parent overlaps a part
    assert post.await_count == 2
    assert all(c.kwargs.get("thread_ts") == "parent" for c in post.await_args_list)
    dele.assert_not_awaited()


async def test_resync_shrinks_message_count():
    # An edit makes the body fit in one message: the parent is updated and the
    # now-surplus continuations are deleted.
    with (
        patch.object(sync_service, "split_for_slack", return_value=["only"]),
        patch.object(sync_service, "_update_attributed_to_github_user", new=AsyncMock()) as upd,
        patch.object(sync_service, "_post_attributed_to_github_user", new=AsyncMock()) as post,
        patch.object(sync_service, "_delete_attributed_to_github_user", new=AsyncMock()) as dele,
    ):
        new_extra = await sync_service._resync_attributed_to_github_user(
            "C1", "parent", ["c1", "c2"], "x" * (SLACK_TEXT_LIMIT + 1), "octo", "xoxb"
        )
    assert new_extra == []
    upd.assert_awaited_once()
    post.assert_not_awaited()
    assert [c.args[1] for c in dele.await_args_list] == ["c1", "c2"]


async def test_issue_comment_deleted_removes_all_chunks():
    payload = {
        "comment": {"id": 7, "user": {"login": "octo"}},
        "issue": {"number": 3, "pull_request": {}},
        "repository": {"full_name": "org/repo"},
    }
    mapping = SimpleNamespace(
        id=1, origin="github", slack_channel_id="C1", slack_ts="1.1", slack_ts_extra=["1.2", "1.3"]
    )
    db_pr = SimpleNamespace(organization_id=10)
    org = SimpleNamespace(slack_bot_token="xoxb")
    with (
        patch.object(
            sync_service,
            "get_message_mapping_by_github_comment",
            new=AsyncMock(return_value=mapping),
        ),
        patch.object(sync_service, "get_pr_by_repo_and_number", new=AsyncMock(return_value=db_pr)),
        patch.object(sync_service, "get_org", new=AsyncMock(return_value=org)),
        patch.object(sync_service, "_delete_attributed_to_github_user", new=AsyncMock()) as dele,
        patch.object(sync_service, "delete_message_mapping", new=AsyncMock()) as del_map,
    ):
        await sync_service.handle_issue_comment_deleted(payload)
    assert [c.args[1] for c in dele.await_args_list] == ["1.1", "1.2", "1.3"]
    del_map.assert_awaited_once_with(1)


# ── Review dismissed ──────────────────────────────────────


async def test_review_dismissed_posts_message_and_clears_state():
    payload = {
        "review": {"user": {"login": "octo"}, "html_url": "https://gh/r"},
        "pull_request": {"number": 5},
        "repository": {"full_name": "org/repo"},
        "sender": {"login": "maintainer"},
    }
    db_pr = SimpleNamespace(organization_id=10, slack_channel_id="C1", github_pr_id=99)
    org = SimpleNamespace(slack_bot_token="xoxb")
    with (
        patch.object(sync_service, "get_pr_by_repo_and_number", new=AsyncMock(return_value=db_pr)),
        patch.object(sync_service, "get_org", new=AsyncMock(return_value=org)),
        patch.object(sync_service.slack_service, "post_message", new=AsyncMock()) as post,
        patch.object(sync_service, "set_pr_last_review_state", new=AsyncMock()) as set_state,
        patch.object(sync_service.pr_digest_service, "update", new=AsyncMock()) as digest,
    ):
        await sync_service.handle_pr_review_dismissed(payload)
    post.assert_awaited_once()
    channel, message = post.await_args.args
    assert channel == "C1"
    assert "maintainer" in message and "octo" in message
    assert "<https://gh/r|org/repo#5>" in message
    set_state.assert_awaited_once_with(99, None)
    digest.assert_awaited_once_with(99, org)


async def test_review_dismissed_skips_untracked_pr():
    payload = {
        "review": {"user": {"login": "octo"}, "html_url": "https://gh/r"},
        "pull_request": {"number": 5},
        "repository": {"full_name": "org/repo"},
        "sender": {"login": "maintainer"},
    }
    with (
        patch.object(sync_service, "get_pr_by_repo_and_number", new=AsyncMock(return_value=None)),
        patch.object(sync_service.slack_service, "post_message", new=AsyncMock()) as post,
    ):
        await sync_service.handle_pr_review_dismissed(payload)
    post.assert_not_called()
