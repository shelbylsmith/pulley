from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services import notification_service

# A real merge commit's SHA, as GitHub stamps onto check_suites for the
# default branch regardless of what actually triggered the run.
_HEAD_SHA = "a4ae34b514252c036981344cf02c11f3b1758a91"


def _check_suite_payload(conclusion="failure", head_branch="main"):
    return {
        "check_suite": {
            "id": 74133876418,
            "status": "completed",
            "conclusion": conclusion,
            "head_branch": head_branch,
            "head_sha": _HEAD_SHA,
            "app": {"name": "GitHub Actions"},
            "pull_requests": [],
        },
        "repository": {
            "full_name": "acme/example-repo",
            "default_branch": "main",
        },
        "installation": {"id": 42},
    }


def _failed_check_run(name, run_id=27548179182):
    return {
        "name": name,
        "conclusion": "failure",
        "html_url": (f"https://github.com/acme/example-repo/actions/runs/{run_id}/job/1"),
    }


_ORG = SimpleNamespace(ci_channel_id="C1", slack_bot_token="xoxb")


def _patches(*, run_event, org=_ORG):
    """Patch the handler's collaborators; the workflow run reports `run_event`."""
    return [
        patch.object(
            notification_service, "get_pr_by_repo_and_number", new=AsyncMock(return_value=None)
        ),
        patch.object(
            notification_service, "get_org_by_installation", new=AsyncMock(return_value=org)
        ),
        patch.object(notification_service, "_commit_mention", new=AsyncMock(return_value=None)),
        patch.object(
            notification_service.github_service,
            "get_check_runs_for_suite",
            new=AsyncMock(return_value=[_failed_check_run("claude-review")]),
        ),
        patch.object(
            notification_service.github_service,
            "get_workflow_run",
            new=AsyncMock(return_value={"name": "Claude Code Review", "event": run_event}),
        ),
    ]


async def test_check_suite_skips_comment_triggered_run():
    """An `@claude` review (issue_comment) that fails is stamped with the
    default-branch HEAD but never validated it — no CI alert should fire."""
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _patches(run_event="issue_comment"):
            p.start()
        try:
            await notification_service.handle_check_suite(_check_suite_payload())
        finally:
            patch.stopall()
        post.assert_not_called()


async def test_check_suite_alerts_on_push_failure():
    """A push to the default branch that breaks CI alerts with the commit."""
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _patches(run_event="push"):
            p.start()
        try:
            await notification_service.handle_check_suite(_check_suite_payload())
        finally:
            patch.stopall()
        post.assert_called_once()
        _, message = post.call_args.args[:2]
        assert "Claude Code Review" in message
        assert _HEAD_SHA[:7] in message
        assert "claude-review" in message


async def test_check_suite_alerts_on_manual_dispatch():
    """workflow_dispatch is branch-scoped — the alert-test harness still fires."""
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _patches(run_event="workflow_dispatch"):
            p.start()
        try:
            await notification_service.handle_check_suite(_check_suite_payload())
        finally:
            patch.stopall()
        post.assert_called_once()


async def test_check_suite_ignores_cancelled_run_on_default_branch():
    """A run cancelled on the default branch (by hand or by a concurrency
    group) never produced a verdict, so no failure alert should fire."""
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _patches(run_event="push"):
            p.start()
        try:
            await notification_service.handle_check_suite(
                _check_suite_payload(conclusion="cancelled")
            )
        finally:
            patch.stopall()
        post.assert_not_called()


# ── Deployment status ─────────────────────────────────────

_JOB_URL = "https://github.com/acme/example-repo/actions/runs/33605621893/job/100168809881"


def _deployment_status_payload(state, log_url=_JOB_URL):
    return {
        "deployment": {
            "ref": "main",
            "sha": _HEAD_SHA,
            "environment": "dev",
            "creator": {"login": "alice"},
        },
        "deployment_status": {"state": state, "description": "", "log_url": log_url},
        "repository": {"full_name": "acme/example-repo", "default_branch": "main"},
        "installation": {"id": 42},
    }


def _deployment_patches(job_conclusion):
    return [
        patch.object(
            notification_service, "get_org_by_installation", new=AsyncMock(return_value=_ORG)
        ),
        patch.object(
            notification_service, "_mention_for_github_login", new=AsyncMock(return_value=None)
        ),
        patch.object(
            notification_service.github_service,
            "get_workflow_job",
            new=AsyncMock(return_value={"conclusion": job_conclusion}),
        ),
    ]


async def test_deployment_error_from_cancelled_job_is_silent():
    """Two deploys to main in quick succession: the concurrency group cancels
    the first, and GitHub reports its deployment as state "error"."""
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _deployment_patches(job_conclusion="cancelled"):
            p.start()
        try:
            await notification_service.handle_deployment_status(_deployment_status_payload("error"))
        finally:
            patch.stopall()
        post.assert_not_called()


async def test_deployment_failure_still_alerts():
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _deployment_patches(job_conclusion="failure"):
            p.start()
        try:
            await notification_service.handle_deployment_status(
                _deployment_status_payload("failure")
            )
        finally:
            patch.stopall()
        post.assert_called_once()
        assert "failure" in post.call_args.args[1]


async def test_deployment_error_without_actions_job_still_alerts():
    """Third-party deployers have no Actions job to consult; error stays error."""
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        patches = _deployment_patches(job_conclusion="cancelled")
        mocks = [p.start() for p in patches]
        try:
            await notification_service.handle_deployment_status(
                _deployment_status_payload("error", log_url="https://deployer.example/123")
            )
        finally:
            patch.stopall()
        post.assert_called_once()
        mocks[-1].assert_not_called()


# ── Check-run rollups ─────────────────────────────────────


def _run(conclusion, status="completed", name="build"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_cancelled_run_is_not_a_failure_in_the_rollup():
    emoji, title, long = notification_service._aggregate_check_state(
        [_run("success", name="lint"), _run("cancelled")]
    )
    assert emoji == "🚫"
    assert title == "CI: 1 cancelled / 2"
    assert long == "1 cancelled, 1 passed"


def test_stale_run_is_not_a_failure_in_the_rollup():
    emoji, _, _ = notification_service._aggregate_check_state([_run("stale")])
    assert emoji == "🚫"


def test_real_failure_still_outranks_cancellation():
    emoji, title, _ = notification_service._aggregate_check_state(
        [_run("cancelled"), _run("failure", name="test")]
    )
    assert emoji == "❌"
    assert title == "CI: 1 failed / 2"


def test_cancelled_suite_yields_no_recap_state():
    """Cancellation is neither a pass nor a fail, so it must not flip the PR's
    last CI state or post a recap."""
    runs = [_run("success", name="lint"), _run("cancelled")]
    assert notification_service._completion_state(runs) is None


def test_failed_suite_with_cancelled_sibling_is_still_failed():
    runs = [_run("cancelled"), _run("failure", name="test")]
    assert notification_service._completion_state(runs) == "failed"


# ── Stale PR reminders ────────────────────────────────────


def _open_pr(number=194, requested=("alice",), draft=False):
    return {
        "number": number,
        "draft": draft,
        "requested_reviewers": [{"login": login} for login in requested],
        "html_url": f"https://github.com/acme/example-repo/pull/{number}",
    }


def _reminder_patches(prs, db_pr, slack_ids=()):
    # send_stale_pr_reminders imports get_open_pulls at call time, so patch it
    # on the github_service module itself.
    return [
        patch.object(
            notification_service.github_service,
            "get_open_pulls",
            new=AsyncMock(return_value=prs),
        ),
        patch.object(
            notification_service, "get_pr_by_repo_and_number", new=AsyncMock(return_value=db_pr)
        ),
        patch.object(notification_service, "set_pr_review_requested_at", new=AsyncMock()),
        patch.object(
            notification_service,
            "get_slack_ids_for_github_usernames",
            new=AsyncMock(return_value=list(slack_ids)),
        ),
    ]


async def _run_reminders():
    await notification_service.send_stale_pr_reminders(42, ["acme/example-repo"], "xoxb")


async def test_stale_reminder_posts_for_aged_pending_request():
    db_pr = SimpleNamespace(
        id=7,
        slack_channel_id="C9",
        review_requested_at=datetime.now(UTC) - timedelta(hours=30),
    )
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _reminder_patches([_open_pr()], db_pr):
            p.start()
        try:
            await _run_reminders()
        finally:
            patch.stopall()
        post.assert_called_once()
        message = post.call_args.args[1]
        assert "30h" in message
        assert "alice" in message


async def test_stale_reminder_mentions_linked_reviewer():
    db_pr = SimpleNamespace(
        id=7,
        slack_channel_id="C9",
        review_requested_at=datetime.now(UTC) - timedelta(hours=48),
    )
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _reminder_patches([_open_pr()], db_pr, slack_ids=["U123"]):
            p.start()
        try:
            await _run_reminders()
        finally:
            patch.stopall()
        assert "<@U123>" in post.call_args.args[1]


async def test_stale_reminder_silent_when_nobody_pending():
    """No requested reviewers → no clock, no nag — however old the PR is."""
    db_pr = SimpleNamespace(
        id=7,
        slack_channel_id="C9",
        review_requested_at=datetime.now(UTC) - timedelta(hours=300),
    )
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _reminder_patches([_open_pr(requested=())], db_pr):
            p.start()
        try:
            await _run_reminders()
        finally:
            patch.stopall()
        post.assert_not_called()


async def test_stale_reminder_waits_out_fresh_request():
    db_pr = SimpleNamespace(
        id=7,
        slack_channel_id="C9",
        review_requested_at=datetime.now(UTC) - timedelta(hours=2),
    )
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _reminder_patches([_open_pr()], db_pr):
            p.start()
        try:
            await _run_reminders()
        finally:
            patch.stopall()
        post.assert_not_called()


async def test_stale_reminder_backfills_missing_clock():
    """Rows that predate the column get stamped now instead of reminded."""
    db_pr = SimpleNamespace(id=7, slack_channel_id="C9", review_requested_at=None)
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _reminder_patches([_open_pr()], db_pr):
            p.start()
        try:
            await _run_reminders()
            notification_service.set_pr_review_requested_at.assert_awaited_once()
            assert notification_service.set_pr_review_requested_at.await_args.args[0] == 7
        finally:
            patch.stopall()
        post.assert_not_called()


async def test_stale_reminder_skips_drafts():
    db_pr = SimpleNamespace(
        id=7,
        slack_channel_id="C9",
        review_requested_at=datetime.now(UTC) - timedelta(hours=100),
    )
    with patch.object(notification_service.slack_service, "post_message", new=AsyncMock()) as post:
        for p in _reminder_patches([_open_pr(draft=True)], db_pr):
            p.start()
        try:
            await _run_reminders()
        finally:
            patch.stopall()
        post.assert_not_called()
