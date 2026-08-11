"""PR digest: one message per PR in the org's PR channel.

Posted once when the PR opens, updated in place on state changes. Uses a
Slack attachment for the colored left-sidebar; blocks for the body layout.

The org's PR channel is a setting that can be repointed. A message ts is only
addressable in the channel it was posted to, so every digest is tracked per
(PR, channel) — see `PRDigestMessage`. After a repoint, each PR moves to the new
channel on its next state change, and messages left in the old channel are
picked up again if the org ever points back at it.
"""

import logging

import slack_sdk.errors

from src.db.queries import get_pr_by_github_id, get_pr_digest_ts, set_pr_digest_ts
from src.models.organization import Organization
from src.models.pull_request import PullRequest
from src.services import slack_service

logger = logging.getLogger(__name__)


# state_key -> (label, emoji, hex color for attachment left-bar)
_STATE_STYLE: dict[str, tuple[str, str, str]] = {
    "draft": ("draft", "📝", "#95a5a6"),
    "reviewable": ("reviewable", "🙏", "#e4b400"),
    "changes_requested": ("changes requested", "🔴", "#e01e5a"),
    "approved": ("approved", "✅", "#2eb886"),
    "merged": ("merged", "🟪", "#8e44ad"),
    "closed": ("closed", "❌", "#7f8c8d"),
}


def _state_key(pr: PullRequest) -> str:
    if pr.state == "merged":
        return "merged"
    if pr.state == "closed":
        return "closed"
    if pr.is_draft:
        return "draft"
    if pr.last_review_state == "approved":
        return "approved"
    if pr.last_review_state == "changes_requested":
        return "changes_requested"
    return "reviewable"


def _short_repo(full_name: str) -> str:
    return full_name.split("/")[-1]


def _render(pr: PullRequest) -> list[dict]:
    """Return the attachments list for a digest message. No top-level text —
    attachments only, so chat.update doesn't trigger the "(edited)" indicator.
    """
    label, emoji, color = _STATE_STYLE[_state_key(pr)]
    channel_ref = f"<#{pr.slack_channel_id}>" if pr.slack_channel_id else "_no channel_"

    header = (
        f"opened <{pr.html_url}|PR #{pr.github_pr_number} {pr.title}> "
        f"on *{_short_repo(pr.repo_full_name)}*"
    )

    reviewers_list = pr.reviewers.split(",") if pr.reviewers else []
    reviewers_text = ", ".join(reviewers_list) if reviewers_list else "_none_"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Channel: {channel_ref}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Author:*\n{pr.author_github_username}"},
                {"type": "mrkdwn", "text": f"*Status:*\n{emoji} {label}"},
                {"type": "mrkdwn", "text": f"*Reviewers:*\n{reviewers_text}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Branch:*\n`{pr.head_branch}` → `{pr.base_branch}`",
                },
            ],
        },
    ]

    attachment = {"color": color, "blocks": blocks}
    return [attachment]


async def post_initial(pr: PullRequest, org: Organization) -> None:
    """Post the digest message when a PR opens. Skipped for draft PRs —
    they'll get posted via the `update` path once the PR is marked ready
    for review. No-op if org has no pr_channel_id.
    """
    if not org.pr_channel_id or not org.slack_bot_token:
        return
    if pr.is_draft:
        logger.info(
            "Skipping digest post for draft PR %s#%d",
            pr.repo_full_name,
            pr.github_pr_number,
        )
        return
    attachments = _render(pr)
    try:
        resp = await slack_service.post_message(
            org.pr_channel_id,
            attachments=attachments,
            token=org.slack_bot_token,
        )
    except slack_sdk.errors.SlackApiError as e:
        # Common causes: bot not in channel (needs chat:write.public or /invite),
        # channel archived, bad channel id. Don't fail the whole webhook handler.
        logger.warning(
            "Digest post failed for %s#%d: %s",
            pr.repo_full_name,
            pr.github_pr_number,
            e.response.get("error"),
        )
        return
    ts = resp.get("ts")
    if ts:
        await set_pr_digest_ts(pr.id, org.pr_channel_id, ts)
        logger.info(
            "Posted PR digest for %s#%d in %s ts=%s",
            pr.repo_full_name,
            pr.github_pr_number,
            org.pr_channel_id,
            ts,
        )


async def update(github_pr_id: int, org: Organization) -> None:
    """Re-render the digest for a PR after a state change.

    Reloads the PR from DB so the caller doesn't need to pass a fresh row.
    If this PR has no digest in the org's *current* PR channel — it opened as a
    draft, the channel was configured after it opened, or the channel has since
    been repointed — post one now, unless the PR is still in draft, in which
    case we keep holding off.
    """
    if not org.pr_channel_id or not org.slack_bot_token:
        return
    pr = await get_pr_by_github_id(github_pr_id)
    if not pr:
        return

    ts = await get_pr_digest_ts(pr.id, org.pr_channel_id)
    if not ts:
        await post_initial(pr, org)
        return

    attachments = _render(pr)
    try:
        await slack_service.update_message(
            org.pr_channel_id,
            ts,
            attachments=attachments,
            token=org.slack_bot_token,
        )
    except slack_sdk.errors.SlackApiError as e:
        logger.warning(
            "Digest update failed for %s#%d: %s",
            pr.repo_full_name,
            pr.github_pr_number,
            e.response.get("error"),
        )
        return
    logger.info(
        "Updated PR digest for %s#%d in %s ts=%s",
        pr.repo_full_name,
        pr.github_pr_number,
        org.pr_channel_id,
        ts,
    )
