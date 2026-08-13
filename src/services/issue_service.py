"""Issue cards: one message per issue in the org's issue channel.

Posted when the issue is first seen, updated in place as it's edited, labelled,
assigned, closed and reopened — the same shape as the PR digest, and for the
same reason: a channel of cards that stay true beats a feed that goes stale.

The org's issue channel is a setting that can be repointed. A message ts is only
addressable in the channel it was posted to, so every card is tracked per
(issue, channel) — see `IssueDigestMessage`. After a repoint, each issue moves to
the new channel on its next event, and cards left in the old channel are picked
up again if the org ever points back at it.
"""

import logging

import slack_sdk.errors

from src.db.queries import (
    delete_issue,
    get_issue_by_github_id,
    get_issue_digest_ts,
    get_org_by_installation,
    set_issue_digest_ts,
    upsert_issue,
)
from src.models.issue import Issue
from src.models.organization import Organization
from src.services import slack_service
from src.services.github_text import github_body_to_slack
from src.utils.markdown import split_for_slack

logger = logging.getLogger(__name__)

# Actions that change what a card shows. Everything else GitHub sends for an
# issue (pinned, locked, milestoned, …) leaves the card as it is.
CARD_ACTIONS = frozenset(
    {
        "opened",
        "edited",
        "closed",
        "reopened",
        "labeled",
        "unlabeled",
        "assigned",
        "unassigned",
    }
)

# state_key -> (label, emoji, hex color for the attachment's left bar)
_STATE_STYLE: dict[str, tuple[str, str, str]] = {
    "open": ("open", "🟢", "#2eb886"),
    "completed": ("closed as completed", "✅", "#8e44ad"),
    "not_planned": ("closed as not planned", "🚫", "#7f8c8d"),
}

# Slack caps a section block's text at 3000 characters, and collapses a long
# message behind its own "show more" control in the client. So send as much of
# the body as a block will hold and let Slack decide what to fold away; only
# what doesn't fit is cut, with the card's title linking to the whole issue.
_SECTION_TEXT_LIMIT = 3000
_TRUNCATION_MARK = "\n…"
_BODY_LIMIT = _SECTION_TEXT_LIMIT - len(_TRUNCATION_MARK)


def _state_key(issue: Issue) -> str:
    if issue.state == "open":
        return "open"
    if issue.state_reason == "not_planned":
        return "not_planned"
    return "completed"


def _short_repo(full_name: str) -> str:
    return full_name.split("/")[-1]


def _body_excerpt(body_slack: str) -> str:
    """Trim rendered mrkdwn to what a section block holds, without splitting a
    code block: `split_for_slack` closes an open fence at the chunk boundary and
    keeps the chunk within the limit, so the first chunk renders on its own.
    """
    if len(body_slack) <= _SECTION_TEXT_LIMIT:
        return body_slack
    return split_for_slack(body_slack, _BODY_LIMIT)[0] + _TRUNCATION_MARK


def _fallback_text(issue: Issue) -> str:
    """Plain-text summary of the card, for notifications and message previews.

    Slack has nothing to put in a notification for a message whose content lives
    in an attachment, which is what leaves the preview blank. The attachment's
    own `fallback` carries it; a top-level `text` would too, but only by
    rendering as a duplicate line of body copy above the card, since `text` is
    hidden only when top-level `blocks` are present and these are not.

    Per Slack's attachment reference this is a plain-text field, so it holds no
    markup — no links, no mrkdwn.
    """
    label, emoji, _ = _STATE_STYLE[_state_key(issue)]
    return (
        f"{emoji} Issue #{issue.github_issue_number} {issue.title} "
        f"({_short_repo(issue.repo_full_name)}) — {label}"
    )


def _render(issue: Issue, body_slack: str) -> list[dict]:
    """Return the attachments list for a card.

    Carries no top-level text: per Slack's chat.update docs it is `text` that
    flips the "(edited)" indicator, and a card re-rendered on every issue event
    would wear it permanently. The preview rides in the attachment instead, so
    it refreshes with the card rather than freezing at whatever the issue was
    when it opened.
    """
    label, emoji, color = _STATE_STYLE[_state_key(issue)]

    header = (
        f"opened <{issue.html_url}|Issue #{issue.github_issue_number} {issue.title}> "
        f"on *{_short_repo(issue.repo_full_name)}*"
    )

    labels = issue.labels.split(",") if issue.labels else []
    labels_text = ", ".join(f"`{name}`" for name in labels) if labels else "_none_"
    assignees = issue.assignees.split(",") if issue.assignees else []
    assignees_text = ", ".join(assignees) if assignees else "_none_"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Author:*\n{issue.author_github_username}"},
                {"type": "mrkdwn", "text": f"*Status:*\n{emoji} {label}"},
                {"type": "mrkdwn", "text": f"*Labels:*\n{labels_text}"},
                {"type": "mrkdwn", "text": f"*Assignees:*\n{assignees_text}"},
            ],
        },
    ]

    if body_slack:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _body_excerpt(body_slack)},
            }
        )

    return [{"color": color, "fallback": _fallback_text(issue), "blocks": blocks}]


def _joined(items: list[dict], key: str) -> str | None:
    """Comma-join a GitHub sub-object list (labels, assignees) for storage."""
    values = [item[key] for item in items]
    return ",".join(values) if values else None


async def _post_or_update(issue: Issue, org: Organization) -> None:
    """Post the card, or update the one already in the org's current channel."""
    body_slack = await github_body_to_slack(issue.body) if issue.body else ""
    attachments = _render(issue, body_slack)
    channel_id = org.issue_channel_id

    ts = await get_issue_digest_ts(issue.id, channel_id)
    try:
        if ts:
            await slack_service.update_message(
                channel_id,
                ts,
                attachments=attachments,
                token=org.slack_bot_token,
            )
        else:
            resp = await slack_service.post_message(
                channel_id,
                attachments=attachments,
                token=org.slack_bot_token,
            )
            ts = resp.get("ts")
            if ts:
                await set_issue_digest_ts(issue.id, channel_id, ts)
    except slack_sdk.errors.SlackApiError as e:
        # Common causes: bot not in the channel, channel archived, bad channel
        # id, or a card deleted by hand (message_not_found on update). Don't
        # fail the webhook over it — GitHub only replays deliveries by hand.
        logger.warning(
            "Issue card failed for %s#%d: %s",
            issue.repo_full_name,
            issue.github_issue_number,
            e.response.get("error"),
        )
        return

    logger.info(
        "Issue card for %s#%d in %s ts=%s",
        issue.repo_full_name,
        issue.github_issue_number,
        channel_id,
        ts,
    )


async def handle_issue_event(payload: dict) -> None:
    """Mirror an issue into the DB and refresh its card.

    The row is written whether or not a channel is configured, so an org that
    sets one later starts from a complete picture of the issues we've seen.
    """
    gh_issue = payload["issue"]
    repo = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]

    org = await get_org_by_installation(installation_id)
    if not org:
        logger.info("Issue event for unknown installation %d", installation_id)
        return

    issue = await upsert_issue(
        organization_id=org.id,
        github_issue_id=gh_issue["id"],
        github_issue_number=gh_issue["number"],
        repo_full_name=repo,
        title=gh_issue["title"],
        body=gh_issue["body"],
        html_url=gh_issue["html_url"],
        author_github_username=gh_issue["user"]["login"],
        state=gh_issue["state"],
        state_reason=gh_issue["state_reason"],
        labels=_joined(gh_issue["labels"], "name"),
        assignees=_joined(gh_issue["assignees"], "login"),
    )

    if not org.issue_channel_id or not org.slack_bot_token:
        return

    await _post_or_update(issue, org)


async def handle_issue_deleted(payload: dict) -> None:
    """Remove the card and the row when an issue is deleted on GitHub.

    Only the card in the org's current channel is removed; cards in channels the
    org has since moved away from are left behind, as they are on a repoint.
    """
    gh_issue = payload["issue"]
    installation_id = payload["installation"]["id"]

    issue = await get_issue_by_github_id(gh_issue["id"])
    if not issue:
        return

    org = await get_org_by_installation(installation_id)
    if org and org.issue_channel_id and org.slack_bot_token:
        ts = await get_issue_digest_ts(issue.id, org.issue_channel_id)
        if ts:
            try:
                await slack_service.delete_message(
                    org.issue_channel_id, ts, token=org.slack_bot_token
                )
            except slack_sdk.errors.SlackApiError as e:
                logger.warning(
                    "Issue card delete failed for %s#%d: %s",
                    issue.repo_full_name,
                    issue.github_issue_number,
                    e.response.get("error"),
                )

    await delete_issue(issue.id)
    logger.info("Issue %s#%d deleted", issue.repo_full_name, issue.github_issue_number)
