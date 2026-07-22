"""Notification service — CI/CD checks, deployments, and daily recaps."""

import logging
import re
from datetime import UTC, datetime

from slack_sdk.errors import SlackApiError

from src.db.queries import (
    get_org,
    get_org_by_installation,
    get_pr_by_repo_and_number,
    get_slack_ids_for_github_usernames,
    set_pr_ci_bookmark_id,
    set_pr_review_requested_at,
    transition_pr_last_ci_state,
)
from src.services import github_service, slack_service
from src.utils.markdown import gfm_to_slack

logger = logging.getLogger(__name__)


# Failure-ish conclusions that should mark the rollup red.
_CI_FAILED = {"failure", "timed_out", "cancelled", "action_required", "stale"}
# Conclusions that count as a clean pass.
_CI_PASSED = {"success", "neutral", "skipped"}

# Workflow-trigger events whose head_sha is a commit on the branch itself —
# a push/merge, a manual dispatch, or a scheduled run. PR- and comment-scoped
# events (issue_comment, pull_request, …) are stamped with the default-branch
# HEAD by GitHub even though the run isn't validating that commit.
_BRANCH_EVENTS = {"push", "workflow_dispatch", "schedule"}


_CHECK_EMOJI = {
    "success": "✅",
    "failure": "❌",
    "timed_out": "⏰",
    "cancelled": "🚫",
    "skipped": "⏭️",
    "neutral": "➖",
    "action_required": "⚠️",
    "stale": "🌫️",
}


def _render_recap_attachment(
    check_runs: list[dict], state: str, title: str, checks_url: str
) -> list[dict]:
    # Sort: failures first, then alphabetical within each group.
    runs_sorted = sorted(
        check_runs,
        key=lambda c: (c.get("conclusion") not in _CI_FAILED, (c.get("name") or "").lower()),
    )

    fields = []
    for c in runs_sorted:
        conclusion = c.get("conclusion") or "unknown"
        icon = _CHECK_EMOJI.get(conclusion, "❔")
        name = c.get("name") or "(unnamed check)"
        url = c.get("html_url") or c.get("details_url")
        value = f"{icon} <{url}|{name}>" if url else f"{icon} {name}"
        fields.append({"value": value, "short": True})

    return [
        {
            "color": "good" if state == "passed" else "danger",
            "fallback": title,
            "title": title,
            "title_link": checks_url,
            "fields": fields,
            "mrkdwn_in": ["fields"],
        }
    ]


def _completion_state(check_runs: list[dict]) -> str | None:
    """Return 'passed'/'failed' once all check-runs are complete, else None.

    Used to gate recap messages: we only post when CI is fully done, and only
    on transitions (passed↔failed) — not every completion event.
    """
    if not check_runs:
        return None
    if any(c.get("status") != "completed" for c in check_runs):
        return None
    if any(c.get("conclusion") in _CI_FAILED for c in check_runs):
        return "failed"
    return "passed"


def _aggregate_check_state(check_runs: list[dict]) -> tuple[str, str, str]:
    """Roll a list of check-runs into (emoji, short_title, long_text).

    Rules: any failure → ❌; otherwise any in-progress/queued → ⏳; otherwise ✅.
    Empty list → ⏳ (waiting for first check to register).
    """
    if not check_runs:
        return "⏳", "CI: waiting", "No checks reported yet"

    failed = sum(1 for c in check_runs if c.get("conclusion") in _CI_FAILED)
    passed = sum(1 for c in check_runs if c.get("conclusion") in _CI_PASSED)
    in_progress = sum(1 for c in check_runs if c.get("status") != "completed")
    total = len(check_runs)

    if failed:
        return "❌", f"CI: {failed} failed / {total}", f"{failed} failed, {passed} passed"
    if in_progress:
        long = f"{in_progress} running, {passed} passed"
        return "⏳", f"CI: {in_progress} running / {total}", long
    return "✅", f"CI: {passed}/{total} passed", f"All {total} checks passed"


# ── CI/CD Check Suites ───────────────────────────────────


async def _refresh_ci_bookmark(
    db_pr,
    installation_id: int,
    head_sha: str,
    bot_token: str | None,
) -> None:
    """Re-roll the PR channel's CI bookmark from current GitHub check-runs."""
    if not db_pr.slack_channel_id:
        return

    try:
        check_runs = await github_service.get_check_runs_for_ref(
            installation_id, db_pr.repo_full_name, head_sha
        )
    except Exception:
        logger.exception("Failed to fetch check-runs for %s@%s", db_pr.repo_full_name, head_sha)
        return

    emoji, title, _ = _aggregate_check_state(check_runs)
    bookmark_title = f"{emoji} {title}"
    checks_url = f"{db_pr.html_url}/checks"

    bookmark_updated_in_place = False
    try:
        if db_pr.ci_bookmark_id:
            await slack_service.edit_bookmark(
                db_pr.slack_channel_id,
                db_pr.ci_bookmark_id,
                title=bookmark_title,
                link=checks_url,
                token=bot_token,
            )
            bookmark_updated_in_place = True
    except SlackApiError as e:
        # Stale id (channel deleted, bookmark removed by hand) → fall through to add.
        if e.response.get("error") not in ("bookmark_not_found", "channel_not_found"):
            raise
        logger.info("Stale ci_bookmark_id for PR %d, recreating", db_pr.id)

    if not bookmark_updated_in_place:
        try:
            bookmark = await slack_service.add_bookmark(
                db_pr.slack_channel_id,
                title=bookmark_title,
                link=checks_url,
                token=bot_token,
            )
        except SlackApiError:
            logger.exception("Failed to add CI bookmark for PR %d", db_pr.id)
            return
        await set_pr_ci_bookmark_id(db_pr.id, bookmark["id"])

    # Post a recap only on completion + state transition. Bookmarks are silent,
    # so without this a passing→failing flip would go unnoticed. The DB-side
    # conditional update dedupes when check_suite/workflow_run events race.
    new_state = _completion_state(check_runs)
    if new_state and await transition_pr_last_ci_state(db_pr.id, new_state):
        recap_title = f"{emoji} {title}"
        try:
            await slack_service.post_message(
                db_pr.slack_channel_id,
                text=recap_title,
                attachments=_render_recap_attachment(
                    check_runs, new_state, recap_title, checks_url
                ),
                token=bot_token,
            )
        except SlackApiError:
            logger.exception("Failed to post CI recap for PR %d", db_pr.id)


async def _mention_for_github_login(login: str | None) -> str | None:
    """Return a Slack <@U…> mention for a GitHub login, or None if unmapped."""
    if not login:
        return None
    slack_ids = await get_slack_ids_for_github_usernames([login])
    if not slack_ids:
        return None
    return f"<@{slack_ids[0]}>"


async def _commit_mention(installation_id: int, repo: str, sha: str) -> str | None:
    """Look up the commit's committer (fallback: author) and resolve to a Slack mention."""
    try:
        commit = await github_service.get_commit(installation_id, repo, sha)
    except Exception:
        logger.exception("Failed to fetch commit %s@%s for mention", repo, sha)
        return None
    for key in ("committer", "author"):
        person = commit.get(key) or {}
        mention = await _mention_for_github_login(person.get("login"))
        if mention:
            return mention
    return None


async def handle_check_suite(payload: dict) -> None:
    check_suite = payload["check_suite"]
    repo = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]
    head_sha = check_suite["head_sha"]

    for pr in check_suite.get("pull_requests", []):
        pr_number = pr["number"]
        db_pr = await get_pr_by_repo_and_number(repo, pr_number)
        if not db_pr or not db_pr.slack_channel_id:
            continue

        org = await get_org(db_pr.organization_id)
        token = org.slack_bot_token if org else None
        await _refresh_ci_bookmark(db_pr, installation_id, head_sha, token)
        logger.info("Refreshed CI bookmark for %s#%d", repo, pr_number)

    # CI-channel alert: failure-ish conclusion on the default branch.
    # check_suite fires per-app, so this covers GitHub Actions and third-party
    # providers (e.g. Cloud Build) uniformly.
    if check_suite.get("status") != "completed":
        return
    conclusion = check_suite.get("conclusion")
    if conclusion not in _CI_FAILED:
        return
    head_branch = check_suite.get("head_branch")
    default_branch = payload["repository"]["default_branch"]
    if head_branch != default_branch:
        return

    org = await get_org_by_installation(installation_id)
    if not org or not org.ci_channel_id:
        return

    icon = _CHECK_EMOJI.get(conclusion, "❌")
    short_sha = head_sha[:7]
    commit_url = f"https://github.com/{repo}/commit/{head_sha}"
    location = f"`{repo}` / `{head_branch}` (<{commit_url}|{short_sha}>)"

    try:
        check_runs = await github_service.get_check_runs_for_suite(
            installation_id, repo, check_suite["id"]
        )
    except Exception:
        logger.exception("Failed to fetch check-runs for suite %s in %s", check_suite["id"], repo)
        check_runs = []

    failed_runs = [r for r in check_runs if r.get("conclusion") in _CI_FAILED]

    # Look up the workflow name and triggering event from any failed run's
    # html_url (.../actions/runs/{run_id}/job/{job_id}). Third-party check_runs
    # that don't match the Actions URL shape fall through to app_name.
    workflow_label = (check_suite.get("app") or {}).get("name") or "CI"
    run_event = None
    for r in failed_runs:
        m = re.search(r"/actions/runs/(\d+)/", r.get("html_url") or "")
        if not m:
            continue
        try:
            wf_run = await github_service.get_workflow_run(installation_id, repo, int(m.group(1)))
            workflow_label = wf_run.get("name") or workflow_label
            run_event = wf_run.get("event")
        except Exception:
            logger.exception("Failed to fetch workflow run %s for %s", m.group(1), repo)
        break

    # A failed run from a PR-/comment-scoped event (e.g. an `@claude` review of
    # an open PR) is stamped with the default-branch HEAD but never validated
    # it; alerting would misreport a CI failure "on main (<sha>)" and tag
    # whoever last pushed. Only branch-scoped events run against the branch
    # commit. Third-party providers expose no workflow_run (run_event stays
    # None) and are push-driven, so we let them through.
    if run_event is not None and run_event not in _BRANCH_EVENTS:
        logger.info(
            "Skipping CI alert for %s suite %s: %s is not a branch-scoped event",
            repo,
            check_suite["id"],
            run_event,
        )
        return

    if len(failed_runs) == 1:
        r = failed_runs[0]
        job_link = f"<{r.get('html_url')}|{r.get('name')}>"
        headline = f"{icon} *{workflow_label}* — job {job_link} failed on {location}"
    elif len(failed_runs) > 1:
        bullets = "\n".join(
            f"• {_CHECK_EMOJI.get(r.get('conclusion'), '❌')} <{r.get('html_url')}|{r.get('name')}>"
            for r in failed_runs
        )
        headline = (
            f"{icon} *{workflow_label}* — {len(failed_runs)} jobs failed on {location}\n{bullets}"
        )
    else:
        headline = f"{icon} *{workflow_label}* {conclusion} on {location}"

    mention = await _commit_mention(installation_id, repo, head_sha)
    message = f"{headline}\ncc {mention}" if mention else headline
    await slack_service.post_message(org.ci_channel_id, message, token=org.slack_bot_token)


# ── Deployment Status ─────────────────────────────────────


async def handle_deployment_status(payload: dict) -> None:
    deployment = payload["deployment"]
    deployment_status = payload["deployment_status"]
    repo = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]

    state = deployment_status["state"]
    if state not in ("failure", "error"):
        return

    ref = deployment.get("ref")
    default_branch = payload["repository"]["default_branch"]
    if ref != default_branch:
        return

    environment = deployment.get("environment", "unknown")
    creator = (deployment.get("creator") or {}).get("login")
    description = deployment_status.get("description", "")

    status_emoji = {"failure": "❌", "error": "💥"}.get(state, "❓")

    org = await get_org_by_installation(installation_id)
    if not org or not org.ci_channel_id:
        logger.info("No CI channel configured for installation %d", installation_id)
        return

    creator_label = creator or "unknown"

    # log_url is the modern field; target_url is the legacy alias.
    log_url = deployment_status.get("log_url") or deployment_status.get("target_url")
    sha = deployment.get("sha")
    sha_link = f"<https://github.com/{repo}/commit/{sha}|{sha[:7]}>" if sha else (ref or "unknown")

    # When the deployment is driven by a GitHub Actions workflow, the payload
    # includes the workflow_run that issued the status update.
    wf_run = payload.get("workflow_run") or {}
    wf_name = wf_run.get("name")
    wf_url = wf_run.get("html_url")
    if wf_name and wf_url:
        workflow_suffix = f" (workflow: <{wf_url}|{wf_name}>)"
    elif wf_name:
        workflow_suffix = f" (workflow: *{wf_name}*)"
    else:
        workflow_suffix = ""

    deployment_label = f"<{log_url}|Deployment>" if log_url else "Deployment"
    header = f"{status_emoji} *{deployment_label}* to `{environment}` — *{state}*{workflow_suffix}"
    message = f"{header}\nRepo: `{repo}` | Ref: `{ref}` ({sha_link}) | By: *{creator_label}*"
    if description:
        message += f"\n> {gfm_to_slack(description)}"

    mention = await _mention_for_github_login(creator)
    if mention:
        message += f"\ncc {mention}"

    await slack_service.post_message(org.ci_channel_id, message, token=org.slack_bot_token)
    logger.info("Deployment %s to %s on %s by %s", state, environment, repo, creator_label)


# ── Workflow Runs (GitHub Actions) ────────────────────────


async def handle_workflow_run(payload: dict) -> None:
    """Notify PR channels and/or the CI channel when a workflow run completes."""
    workflow_run = payload["workflow_run"]
    repo = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]

    workflow_name = workflow_run["name"]

    # Refresh the per-PR CI bookmark instead of posting a message per workflow run.
    # Default-branch CI failures are posted from handle_check_suite (covers both
    # GitHub Actions and third-party providers like Cloud Build).
    pull_requests = workflow_run.get("pull_requests", [])
    full_head_sha = workflow_run["head_sha"]
    for pr in pull_requests:
        pr_number = pr["number"]
        db_pr = await get_pr_by_repo_and_number(repo, pr_number)
        if not db_pr or not db_pr.slack_channel_id:
            continue

        org = await get_org(db_pr.organization_id)
        token = org.slack_bot_token if org else None

        await _refresh_ci_bookmark(db_pr, installation_id, full_head_sha, token)
        logger.info(
            "Refreshed CI bookmark via workflow %s for %s#%d",
            workflow_name,
            repo,
            pr_number,
        )


# ── Per-org job entrypoints ───────────────────────────────


async def run_recap_for_org(org) -> None:
    """List the org's installed repos and post its daily recap."""
    repos = await github_service.list_installation_repos(org.github_installation_id)
    repo_names = [r["full_name"] for r in repos]
    await send_daily_recap(
        org.github_installation_id,
        repo_names,
        org.recap_channel_id,
        org.slack_bot_token,
    )


async def run_stale_reminders_for_org(org) -> None:
    """List the org's installed repos and post its stale-PR reminders."""
    repos = await github_service.list_installation_repos(org.github_installation_id)
    repo_names = [r["full_name"] for r in repos]
    await send_stale_pr_reminders(
        org.github_installation_id,
        repo_names,
        org.slack_bot_token,
    )


# ── Daily PR Recap ────────────────────────────────────────


async def send_daily_recap(
    installation_id: int,
    repos: list[str],
    slack_channel_id: str,
    bot_token: str,
) -> None:
    from src.services.github_service import get_open_pulls

    all_prs: list[dict] = []
    for repo in repos:
        prs = await get_open_pulls(installation_id, repo)
        all_prs.extend(prs)

    if not all_prs:
        await slack_service.post_message(
            slack_channel_id,
            "📋 *Daily PR Recap* — No open pull requests. 🎉",
            token=bot_token,
        )
        return

    now = datetime.now(UTC)

    stale: list[dict] = []
    needs_review: list[dict] = []
    drafts: list[dict] = []

    for pr in all_prs:
        updated = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
        age_hours = (now - updated).total_seconds() / 3600

        if pr.get("draft"):
            drafts.append(pr)
        elif age_hours > 48:
            stale.append(pr)
        else:
            needs_review.append(pr)

    def _pr_line(pr: dict) -> str:
        url, num = pr["html_url"], pr["number"]
        return f"  • <{url}|#{num}> {pr['title']} — @{pr['user']['login']}"

    lines = ["📋 *Daily PR Recap*\n"]

    if stale:
        lines.append(f"*🔴 Stale ({len(stale)}):*")
        lines.extend(_pr_line(pr) for pr in stale)

    if needs_review:
        lines.append(f"\n*🟡 Needs Review ({len(needs_review)}):*")
        lines.extend(_pr_line(pr) for pr in needs_review)

    if drafts:
        lines.append(f"\n*📝 Drafts ({len(drafts)}):*")
        lines.extend(_pr_line(pr) for pr in drafts)

    lines.append(f"\n_Total: {len(all_prs)} open PRs_")

    await slack_service.post_message(
        slack_channel_id,
        "\n".join(lines),
        token=bot_token,
    )

    logger.info("Sent daily recap: %d PRs to %s", len(all_prs), slack_channel_id)


# ── Stale PR Reminders ────────────────────────────────────


async def send_stale_pr_reminders(
    installation_id: int,
    repos: list[str],
    bot_token: str,
) -> None:
    from src.services.github_service import get_open_pulls

    now = datetime.now(UTC)

    for repo in repos:
        prs = await get_open_pulls(installation_id, repo)
        for pr in prs:
            if pr.get("draft"):
                continue

            # GitHub drops a reviewer from requested_reviewers the moment they
            # submit and re-adds them on re-request, so this is the live
            # pending set — no review fetching needed.
            pending = [r["login"] for r in pr.get("requested_reviewers", [])]
            if not pending:
                continue

            db_pr = await get_pr_by_repo_and_number(repo, pr["number"])
            if not db_pr or not db_pr.slack_channel_id:
                continue

            if db_pr.review_requested_at is None:
                # Row predates the pending-review clock; start it now and
                # remind on a later run once it has actually aged.
                await set_pr_review_requested_at(db_pr.id, now)
                continue

            hours_pending = (now - db_pr.review_requested_at).total_seconds() / 3600
            if hours_pending < 24:
                continue

            labels = []
            for login in pending:
                mention = await _mention_for_github_login(login)
                labels.append(mention or f"*{login}*")

            hours = int(hours_pending)
            await slack_service.post_message(
                db_pr.slack_channel_id,
                f"⏰ Review requested {hours}h ago — still waiting on {', '.join(labels)}.",
                token=bot_token,
            )
            logger.info(
                "Sent stale reminder for %s#%d (%dh pending)",
                repo,
                pr["number"],
                hours,
            )
