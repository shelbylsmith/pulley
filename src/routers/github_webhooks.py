"""GitHub webhook endpoint — receives events from the GitHub App and dispatches them."""

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from src.config import settings
from src.utils.webhook_verify import verify_github_signature

logger = logging.getLogger(__name__)
router = APIRouter()


async def _dispatch_event(event: str, action: str | None, payload: dict) -> None:
    """Route a GitHub webhook event to the appropriate handler."""
    from src.services.channel_manager import (
        handle_pr_closed,
        handle_pr_opened,
        handle_pr_reopened,
        handle_pr_updated,
        handle_reviewer_removed,
        handle_reviewer_requested,
    )
    from src.services.notification_service import (
        handle_check_suite,
        handle_deployment_status,
    )
    from src.services.sync_service import (
        handle_issue_comment,
        handle_issue_comment_deleted,
        handle_issue_comment_edited,
        handle_pr_review,
        handle_pr_review_dismissed,
        handle_review_comment,
        handle_review_comment_deleted,
        handle_review_comment_edited,
        handle_review_edited,
    )

    if event == "pull_request":
        if action == "opened":
            await handle_pr_opened(payload)
        elif action == "reopened":
            await handle_pr_reopened(payload)
        elif action == "closed":
            await handle_pr_closed(payload)
        elif action in ("edited", "converted_to_draft", "ready_for_review", "synchronize"):
            await handle_pr_updated(payload, action)
        elif action == "review_requested":
            await handle_reviewer_requested(payload)
        elif action == "review_request_removed":
            await handle_reviewer_removed(payload)

    elif event == "pull_request_review":
        if action == "submitted":
            await handle_pr_review(payload)
        elif action == "edited":
            await handle_review_edited(payload)
        elif action == "dismissed":
            await handle_pr_review_dismissed(payload)

    elif event == "pull_request_review_comment":
        if action == "created":
            await handle_review_comment(payload)
        elif action == "edited":
            await handle_review_comment_edited(payload)
        elif action == "deleted":
            await handle_review_comment_deleted(payload)

    elif event == "pull_request_review_thread":
        from src.services.sync_service import handle_review_thread

        if action in ("resolved", "unresolved"):
            await handle_review_thread(payload, action)

    elif event == "issue_comment":
        if action == "created":
            await handle_issue_comment(payload)
        elif action == "edited":
            await handle_issue_comment_edited(payload)
        elif action == "deleted":
            await handle_issue_comment_deleted(payload)

    elif event == "check_suite":
        if action == "completed":
            await handle_check_suite(payload)

    elif event == "deployment_status":
        await handle_deployment_status(payload)

    elif event == "workflow_run":
        if action == "completed":
            from src.services.notification_service import handle_workflow_run

            await handle_workflow_run(payload)

    elif event == "installation":
        from src.services.installation_service import handle_installation

        await handle_installation(payload, action)


@router.post("")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(...),
    x_github_delivery: str = Header(...),
):
    body = await request.body()

    if not verify_github_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    action = payload.get("action")

    logger.info("GitHub event=%s action=%s delivery=%s", x_github_event, action, x_github_delivery)

    # Installation events aren't repo-scoped (they carry a `repositories` list,
    # not a single `repository`), so the per-repo allow/exclude gates skip them.
    if not x_github_event.startswith("installation"):
        repo_name = payload.get("repository", {}).get("full_name")
        if repo_name:
            if repo_name in settings.excluded_repos:
                logger.info(
                    "Skipping %s for %s (in GITHUB_EXCLUDED_REPOS)",
                    x_github_event,
                    repo_name,
                )
                return {"ok": True, "skipped": True}
            allowed = settings.allowed_repos
            if allowed and repo_name not in allowed:
                logger.info(
                    "Skipping %s for %s (not in GITHUB_ALLOWED_REPOS)",
                    x_github_event,
                    repo_name,
                )
                return {"ok": True, "skipped": True}

    await _dispatch_event(x_github_event, action, payload)
    return {"ok": True}
