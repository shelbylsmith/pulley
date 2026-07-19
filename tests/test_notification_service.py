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
