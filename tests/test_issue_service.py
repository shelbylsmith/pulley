"""Tests for issue cards: rendering, the update-in-place lifecycle, channel
repointing, gating, and event routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import slack_sdk.errors

from src.routers import github_webhooks
from src.services import issue_service


def _payload(**issue_overrides):
    issue = {
        "id": 900001,
        "number": 42,
        "title": "Parser drops trailing newlines",
        "html_url": "https://github.com/acme/widgets/issues/42",
        "body": "Steps to reproduce:\n\n1. Parse a file",
        "user": {"login": "octocat"},
        "state": "open",
        "state_reason": None,
        "labels": [{"name": "bug"}, {"name": "p1"}],
        "assignees": [{"login": "hubot"}],
    }
    issue.update(issue_overrides)
    return {
        "issue": issue,
        "repository": {"full_name": "acme/widgets"},
        "installation": {"id": 42},
    }


def _org(**overrides):
    fields = {"id": 1, "issue_channel_id": "C_ISSUES", "slack_bot_token": "xoxb"}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _issue(**overrides):
    fields = {
        "id": 7,
        "github_issue_number": 42,
        "repo_full_name": "acme/widgets",
        "title": "Parser drops trailing newlines",
        "body": "Steps to reproduce",
        "html_url": "https://github.com/acme/widgets/issues/42",
        "author_github_username": "octocat",
        "state": "open",
        "state_reason": None,
        "labels": "bug,p1",
        "assignees": "hubot",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Recorder:
    """Stands in for the DB and Slack, capturing what the handler did."""

    def __init__(self, org, existing_ts=None):
        self.org = org
        self.existing_ts = existing_ts
        self.upserts = []
        self.saved_ts = []
        self.deleted_issues = []
        self.post = AsyncMock(return_value={"ts": "1700000000.000100"})
        self.update = AsyncMock()
        self.delete = AsyncMock()

    def patches(self):
        async def fake_upsert(**fields):
            self.upserts.append(fields)
            return _issue(
                title=fields["title"],
                body=fields["body"],
                state=fields["state"],
                state_reason=fields["state_reason"],
                labels=fields["labels"],
                assignees=fields["assignees"],
            )

        async def fake_get_ts(issue_id, channel_id):
            return self.existing_ts

        async def fake_set_ts(issue_id, channel_id, ts):
            self.saved_ts.append((issue_id, channel_id, ts))

        async def fake_delete_issue(issue_id):
            self.deleted_issues.append(issue_id)

        return [
            patch.object(
                issue_service, "get_org_by_installation", new=AsyncMock(return_value=self.org)
            ),
            patch.object(issue_service, "upsert_issue", new=fake_upsert),
            patch.object(
                issue_service, "get_issue_by_github_id", new=AsyncMock(return_value=_issue())
            ),
            patch.object(issue_service, "get_issue_digest_ts", new=fake_get_ts),
            patch.object(issue_service, "set_issue_digest_ts", new=fake_set_ts),
            patch.object(issue_service, "delete_issue", new=fake_delete_issue),
            patch.object(
                issue_service, "github_body_to_slack", new=AsyncMock(side_effect=lambda b: b)
            ),
            patch.object(issue_service.slack_service, "post_message", new=self.post),
            patch.object(issue_service.slack_service, "update_message", new=self.update),
            patch.object(issue_service.slack_service, "delete_message", new=self.delete),
        ]


async def _run(handler, payload, org, existing_ts=None):
    rec = _Recorder(org, existing_ts=existing_ts)
    started = rec.patches()
    for p in started:
        p.start()
    try:
        await handler(payload)
    finally:
        for p in started:
            p.stop()
    return rec


def _card_text(attachments):
    """Flatten a card's blocks into searchable text."""
    parts = []
    for block in attachments[0]["blocks"]:
        if "text" in block:
            parts.append(block["text"]["text"])
        for field in block.get("fields", []):
            parts.append(field["text"])
    return "\n".join(parts)


# ── Rendering ─────────────────────────────────────────────


def test_card_shows_the_issue_and_its_metadata():
    attachments = issue_service._render(_issue(), "Steps to reproduce")

    text = _card_text(attachments)
    assert "Issue #42 Parser drops trailing newlines" in text
    assert "octocat" in text
    assert "🟢 open" in text
    assert "`bug`, `p1`" in text
    assert "hubot" in text
    assert "Steps to reproduce" in text


def test_card_marks_unlabelled_and_unassigned_issues_as_none():
    attachments = issue_service._render(_issue(labels=None, assignees=None), "")

    text = _card_text(attachments)
    assert "*Labels:*\n_none_" in text
    assert "*Assignees:*\n_none_" in text


@pytest.mark.parametrize(
    ("state", "reason", "expected"),
    [
        ("open", None, "🟢 open"),
        ("open", "reopened", "🟢 open"),
        ("closed", "completed", "✅ closed as completed"),
        ("closed", "not_planned", "🚫 closed as not planned"),
        # Issues closed before GitHub recorded a reason report none.
        ("closed", None, "✅ closed as completed"),
    ],
)
def test_card_status_tracks_state_and_reason(state, reason, expected):
    attachments = issue_service._render(_issue(state=state, state_reason=reason), "")

    assert expected in _card_text(attachments)


def _body_block(attachments):
    return attachments[0]["blocks"][-1]["text"]["text"]


def test_a_body_that_fits_a_section_block_is_sent_whole():
    """Slack collapses what's too long to show; we only cut what won't fit."""
    body = "x" * 2999

    attachments = issue_service._render(_issue(), body)

    assert _body_block(attachments) == body


def test_a_body_too_long_for_a_section_block_is_cut_to_fit():
    body = "\n".join(f"line {i}" for i in range(500))
    assert len(body) > 3000

    attachments = issue_service._render(_issue(), body)

    block = _body_block(attachments)
    assert len(block) <= 3000
    assert block.endswith("…")
    assert block.startswith("line 0")


def test_cutting_a_body_does_not_leave_a_code_fence_open():
    body = "```\n" + "\n".join(f"line {i}" for i in range(500)) + "\n```"

    attachments = issue_service._render(_issue(), body)

    block = _body_block(attachments)
    assert len(block) <= 3000
    assert block.count("```") % 2 == 0


# ── Lifecycle ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_event_posts_a_card_and_records_its_ts():
    rec = await _run(issue_service.handle_issue_event, _payload(), _org())

    rec.post.assert_awaited_once()
    rec.update.assert_not_awaited()
    assert rec.post.await_args.args[0] == "C_ISSUES"
    assert rec.post.await_args.kwargs["token"] == "xoxb"
    assert rec.saved_ts == [(7, "C_ISSUES", "1700000000.000100")]


@pytest.mark.asyncio
async def test_later_event_updates_the_existing_card_in_place():
    payload = _payload(state="closed", state_reason="completed")

    rec = await _run(issue_service.handle_issue_event, payload, _org(), existing_ts="111.222")

    rec.post.assert_not_awaited()
    rec.update.assert_awaited_once()
    channel, ts = rec.update.await_args.args
    assert (channel, ts) == ("C_ISSUES", "111.222")
    assert "✅ closed as completed" in _card_text(rec.update.await_args.kwargs["attachments"])


@pytest.mark.asyncio
async def test_update_carries_no_text_so_slack_shows_no_edited_marker():
    rec = await _run(issue_service.handle_issue_event, _payload(), _org(), existing_ts="111.222")

    assert "text" not in rec.update.await_args.kwargs


@pytest.mark.asyncio
async def test_every_event_mirrors_the_issue_to_the_database():
    payload = _payload(state="closed", state_reason="not_planned", labels=[{"name": "wontfix"}])

    rec = await _run(issue_service.handle_issue_event, payload, _org())

    assert rec.upserts == [
        {
            "organization_id": 1,
            "github_issue_id": 900001,
            "github_issue_number": 42,
            "repo_full_name": "acme/widgets",
            "title": "Parser drops trailing newlines",
            "body": "Steps to reproduce:\n\n1. Parse a file",
            "html_url": "https://github.com/acme/widgets/issues/42",
            "author_github_username": "octocat",
            "state": "closed",
            "state_reason": "not_planned",
            "labels": "wontfix",
            "assignees": "hubot",
        }
    ]


@pytest.mark.asyncio
async def test_issue_is_mirrored_even_with_no_channel_configured():
    """So an org that configures a channel later starts from a full picture."""
    rec = await _run(issue_service.handle_issue_event, _payload(), _org(issue_channel_id=None))

    assert len(rec.upserts) == 1
    rec.post.assert_not_awaited()
    rec.update.assert_not_awaited()


# ── Channel repointing ────────────────────────────────────


@pytest.mark.asyncio
async def test_repointed_channel_gets_a_fresh_card():
    """No ts for the *current* channel — even though the issue has a card in the
    channel the org moved away from — so post rather than update."""
    rec = await _run(
        issue_service.handle_issue_event,
        _payload(),
        _org(issue_channel_id="C_NEW"),
        existing_ts=None,
    )

    rec.post.assert_awaited_once()
    assert rec.post.await_args.args[0] == "C_NEW"
    assert rec.saved_ts == [(7, "C_NEW", "1700000000.000100")]


@pytest.mark.asyncio
async def test_ts_is_looked_up_per_channel():
    rec = _Recorder(_org(issue_channel_id="C_NEW"))
    seen = []

    async def fake_get_ts(issue_id, channel_id):
        seen.append((issue_id, channel_id))
        return None

    started = rec.patches()
    for p in started:
        p.start()
    try:
        with patch.object(issue_service, "get_issue_digest_ts", new=fake_get_ts):
            await issue_service.handle_issue_event(_payload())
    finally:
        for p in started:
            p.stop()

    assert seen == [(7, "C_NEW")]


# ── Gating and failure ────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_installation_is_dropped():
    rec = await _run(issue_service.handle_issue_event, _payload(), None)

    assert rec.upserts == []
    rec.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_failure_does_not_propagate():
    """A failed card must not 500 the webhook — GitHub only replays by hand."""
    rec = _Recorder(_org())
    rec.post = AsyncMock(
        side_effect=slack_sdk.errors.SlackApiError("not_in_channel", {"error": "not_in_channel"})
    )
    started = rec.patches()
    for p in started:
        p.start()
    try:
        await issue_service.handle_issue_event(_payload())
    finally:
        for p in started:
            p.stop()

    rec.post.assert_awaited_once()
    assert rec.saved_ts == []


# ── Deletion ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_an_issue_removes_its_card_and_row():
    rec = await _run(issue_service.handle_issue_deleted, _payload(), _org(), existing_ts="111.222")

    rec.delete.assert_awaited_once_with("C_ISSUES", "111.222", token="xoxb")
    assert rec.deleted_issues == [7]


@pytest.mark.asyncio
async def test_deleting_an_issue_with_no_card_still_drops_the_row():
    rec = await _run(issue_service.handle_issue_deleted, _payload(), _org(), existing_ts=None)

    rec.delete.assert_not_awaited()
    assert rec.deleted_issues == [7]


# ── Routing ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("action", sorted(issue_service.CARD_ACTIONS))
async def test_card_actions_reach_the_handler(action):
    with patch("src.services.issue_service.handle_issue_event", new=AsyncMock()) as handler:
        await github_webhooks._dispatch_event("issues", action, _payload())

    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_deleted_routes_to_the_deletion_handler():
    with (
        patch("src.services.issue_service.handle_issue_deleted", new=AsyncMock()) as deleted,
        patch("src.services.issue_service.handle_issue_event", new=AsyncMock()) as updated,
    ):
        await github_webhooks._dispatch_event("issues", "deleted", _payload())

    deleted.assert_awaited_once()
    updated.assert_not_awaited()


@pytest.mark.asyncio
async def test_actions_that_do_not_change_the_card_are_ignored():
    with (
        patch("src.services.issue_service.handle_issue_event", new=AsyncMock()) as updated,
        patch("src.services.issue_service.handle_issue_deleted", new=AsyncMock()) as deleted,
    ):
        for action in ("pinned", "unpinned", "locked", "unlocked", "milestoned", "transferred"):
            await github_webhooks._dispatch_event("issues", action, _payload())

    updated.assert_not_awaited()
    deleted.assert_not_awaited()
