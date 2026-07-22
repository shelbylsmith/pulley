"""Bidirectional comment sync between GitHub and Slack.

GitHub → Slack: code review comments and issue comments appear as Slack messages/threads.
Slack → GitHub: channel messages are posted as issue comments on the PR.

Echo prevention: messages originating from the sync are tagged so they can be ignored
when they bounce back through the other platform's webhook.
"""

import logging

import slack_sdk.errors

from src.db.queries import (
    create_message_mapping,
    create_thread_mapping,
    delete_message_mapping,
    get_github_username_map_for_slack_ids,
    get_message_mapping_by_github_comment,
    get_message_mapping_by_slack_ts,
    get_org,
    get_pr_by_channel,
    get_pr_by_repo_and_number,
    get_slack_id_map_for_github_usernames,
    get_slack_ids_for_github_usernames,
    get_thread_mapping,
    get_thread_mapping_by_slack_ts,
    get_user_by_github_username,
    get_user_by_slack_id,
    set_message_mapping_extra_ts,
    set_pr_last_review_state,
    set_pr_reviewers,
)
from src.services import pr_digest_service, slack_service
from src.services.github_service import (
    create_issue_comment,
    create_issue_comment_as_user,
    delete_issue_comment,
    delete_issue_comment_as_user,
    delete_review_comment,
    delete_review_comment_as_user,
    get_valid_user_token,
    reply_to_review_comment_as_user,
    update_issue_comment,
    update_issue_comment_as_user,
    update_review_comment,
    update_review_comment_as_user,
)
from src.utils.markdown import (
    SLACK_TEXT_LIMIT,
    blockquote,
    gfm_to_slack,
    github_mention_logins,
    slack_mention_ids,
    slack_to_gfm,
    split_for_slack,
)

logger = logging.getLogger(__name__)

SYNC_TAG = "<!-- pulley-sync -->"
SLACK_SYNC_PREFIX = "[via Slack]"


async def _github_body_to_slack(body: str) -> str:
    """Render a GitHub comment body as Slack mrkdwn, turning @mentions of linked
    users into Slack pings. Unlinked logins stay literal.
    """
    logins = github_mention_logins(body)
    mentions = await get_slack_id_map_for_github_usernames(logins) if logins else {}
    return gfm_to_slack(body, mentions)


_REVIEW_STATE_DISPLAY = {
    "approved": "✅ approved",
    "changes_requested": "🔴 requested changes",
    "commented": "💬 commented",
}


def _render_review_message(
    state: str, review_url: str, repo: str, pr_number: int, body_slack: str
) -> str:
    state_display = _REVIEW_STATE_DISPLAY.get(state, state)
    message = f"{state_display} on <{review_url}|{repo}#{pr_number}>"
    if body_slack:
        message += f"\n{blockquote(body_slack)}"
    return message


def _render_review_comment_message(comment: dict, body_slack: str) -> str:
    """Rich rendering for a review thread's root comment (file location + diff)."""
    path = comment.get("path", "")
    line = comment.get("line") or comment.get("original_line", "")
    diff_hunk = comment.get("diff_hunk", "")
    comment_url = comment["html_url"]

    location = f"`{path}"
    if line:
        location += f":{line}"
    location += "`"

    message = f"commented on {location} (<{comment_url}|view>)\n"
    if diff_hunk:
        hunk_lines = diff_hunk.strip().split("\n")[-3:]
        message += "```\n" + "\n".join(hunk_lines) + "\n```\n"
    if body_slack:
        message += blockquote(body_slack)
    return message


def _render_issue_comment_message(
    comment_url: str, repo: str, issue_number: int, body_slack: str
) -> str:
    quoted = blockquote(body_slack)
    return f"<{comment_url}|commented> on {repo}#{issue_number}:\n{quoted}"


# ── GitHub → Slack ────────────────────────────────────────


async def handle_pr_review(payload: dict) -> None:
    review = payload["review"]
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]
    reviewer = review["user"]["login"]

    # Submitting a review removes the submitter from requested_reviewers, and
    # the payload's PR object already reflects that — sync it (filtering the
    # submitter defensively in case the embedded object is stale) so the
    # pending-review clock stops when nobody is left pending.
    await set_pr_reviewers(
        pr["id"],
        [r["login"] for r in pr.get("requested_reviewers", []) if r["login"] != reviewer],
    )

    body = review.get("body") or ""
    if SYNC_TAG in body:
        return

    state = review["state"]
    # A review with state=commented and no body is just the wrapper around
    # inline code comments — those each arrive via pull_request_review_comment.
    # Skip to avoid a noisy duplicate "commented on PR" message alongside the
    # actual content.
    if state == "commented" and not body:
        return

    db_pr = await get_pr_by_repo_and_number(repo, pr_number)
    if not db_pr or not db_pr.slack_channel_id:
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None

    review_url = review["html_url"]

    body_slack = await _github_body_to_slack(body) if body else ""
    message = _render_review_message(state, review_url, repo, pr_number, body_slack)

    ts_list = await _post_attributed_to_github_user(
        db_pr.slack_channel_id, message, reviewer, token
    )
    if ts_list:
        await create_message_mapping(
            pull_request_id=db_pr.id,
            slack_channel_id=db_pr.slack_channel_id,
            slack_ts=ts_list[0],
            github_comment_id=review["id"],
            github_comment_type="review",
            origin="github",
            slack_ts_extra=ts_list[1:],
        )

    # Update the org-level digest with the new review state

    if state in ("approved", "changes_requested"):
        await set_pr_last_review_state(db_pr.github_pr_id, state)
        if org:
            await pr_digest_service.update(db_pr.github_pr_id, org)

    logger.info("Review %s by %s on %s#%d", state, reviewer, repo, pr_number)


async def handle_pr_review_dismissed(payload: dict) -> None:
    """Notify the PR channel when a submitted review is dismissed.

    A dismissal drops the review's approval / changes-requested standing, so we
    also clear the stored last-review state and refresh the digest.
    """
    review = payload["review"]
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]

    # Dismissal doesn't re-open the review request, but the payload carries
    # the current pending set — keep the stored column in sync.
    await set_pr_reviewers(pr["id"], [r["login"] for r in pr.get("requested_reviewers", [])])

    db_pr = await get_pr_by_repo_and_number(repo, pr_number)
    if not db_pr or not db_pr.slack_channel_id:
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None

    reviewer = review["user"]["login"]
    dismisser = payload.get("sender", {}).get("login", "someone")
    review_url = review["html_url"]

    message = (
        f"🚫 *{dismisser}* dismissed *{reviewer}*'s review on <{review_url}|{repo}#{pr_number}>"
    )
    await slack_service.post_message(db_pr.slack_channel_id, message, token=token)

    await set_pr_last_review_state(db_pr.github_pr_id, None)
    if org:
        await pr_digest_service.update(db_pr.github_pr_id, org)

    logger.info("Review by %s dismissed by %s on %s#%d", reviewer, dismisser, repo, pr_number)


async def handle_review_comment(payload: dict) -> None:
    comment = payload["comment"]
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]

    body = comment.get("body", "")
    if SYNC_TAG in body:
        return

    db_pr = await get_pr_by_repo_and_number(repo, pr_number)
    if not db_pr or not db_pr.slack_channel_id:
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None

    author = comment["user"]["login"]
    path = comment.get("path", "")

    # GitHub groups review comments into threads via in_reply_to_id.
    # The first comment in a thread has no in_reply_to_id — it becomes the Slack parent.
    # Replies have in_reply_to_id pointing to the root comment.
    in_reply_to = comment.get("in_reply_to_id")
    github_thread_id = str(in_reply_to or comment["id"])

    # Check if we already have a Slack thread for this conversation
    mapping = await get_thread_mapping(db_pr.id, github_thread_id)

    ts_list: list[str] = []

    if mapping:
        reply_msg = await _github_body_to_slack(body) if body else "replied"
        try:
            ts_list = await _post_attributed_to_github_user(
                db_pr.slack_channel_id,
                reply_msg,
                author,
                token,
                thread_ts=mapping.slack_thread_ts,
            )
        except slack_sdk.errors.SlackApiError as e:
            error_code = e.response.get("error", "")
            if error_code in ("thread_not_found", "message_not_found", "channel_not_found"):
                logger.warning(
                    "Thread %s gone (%s), posting as new message",
                    mapping.slack_thread_ts,
                    error_code,
                )
            else:
                raise

    if not ts_list:
        body_slack = await _github_body_to_slack(body) if body else ""
        message = _render_review_comment_message(comment, body_slack)

        ts_list = await _post_attributed_to_github_user(
            db_pr.slack_channel_id, message, author, token
        )

        if ts_list:
            await create_thread_mapping(
                pull_request_id=db_pr.id,
                github_thread_id=github_thread_id,
                slack_channel_id=db_pr.slack_channel_id,
                slack_thread_ts=ts_list[0],
                file_path=path,
            )

    if ts_list:
        await create_message_mapping(
            pull_request_id=db_pr.id,
            slack_channel_id=db_pr.slack_channel_id,
            slack_ts=ts_list[0],
            github_comment_id=comment["id"],
            github_comment_type="review_comment",
            origin="github",
            slack_ts_extra=ts_list[1:],
        )

    logger.info(
        "Review comment by %s on %s#%d at %s (thread=%s)",
        author,
        repo,
        pr_number,
        path,
        github_thread_id,
    )


async def handle_review_thread(payload: dict, action: str) -> None:
    """Notify the PR channel when a review thread is resolved or unresolved."""
    thread = payload["thread"]
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]

    db_pr = await get_pr_by_repo_and_number(repo, pr_number)
    if not db_pr or not db_pr.slack_channel_id:
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None

    first_comment = thread["comments"][0] if thread.get("comments") else {}
    path = first_comment.get("path", "")
    resolver = payload.get("sender", {}).get("login", "someone")

    if action == "resolved":
        emoji = "✅"
        verb = "resolved"
    else:
        emoji = "🔄"
        verb = "unresolved"

    message = f"{emoji} *{resolver}* {verb} a review thread"
    if path:
        message += f" on `{path}`"

    await slack_service.post_message(db_pr.slack_channel_id, message, token=token)
    logger.info("Review thread %s by %s on %s#%d", verb, resolver, repo, pr_number)


async def handle_issue_comment(payload: dict) -> None:
    comment = payload["comment"]
    issue = payload["issue"]

    if "pull_request" not in issue:
        return

    body = comment.get("body", "")
    if SYNC_TAG in body:
        return

    author = comment["user"]["login"]
    repo = payload["repository"]["full_name"]
    issue_number = issue["number"]
    comment_url = comment["html_url"]

    db_pr = await get_pr_by_repo_and_number(repo, issue_number)
    if not db_pr or not db_pr.slack_channel_id:
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None

    body_slack = await _github_body_to_slack(body)
    message = _render_issue_comment_message(comment_url, repo, issue_number, body_slack)
    ts_list = await _post_attributed_to_github_user(db_pr.slack_channel_id, message, author, token)
    if ts_list:
        await create_message_mapping(
            pull_request_id=db_pr.id,
            slack_channel_id=db_pr.slack_channel_id,
            slack_ts=ts_list[0],
            github_comment_id=comment["id"],
            github_comment_type="issue_comment",
            origin="github",
            slack_ts_extra=ts_list[1:],
        )
    logger.info("Issue comment by %s on %s#%d", author, repo, issue_number)


# ── GitHub → Slack: edits & deletions ─────────────────────


async def _mapping_and_token_for_github_comment(
    payload: dict, comment_id: int, comment_type: str, pr_number: int
):
    """Resolve the (mapping, db_pr, bot_token) for a GitHub edit/delete event,
    or None if this comment wasn't synced from GitHub. Skips slack-origin rows so
    that our own edits/deletes don't bounce back.
    """
    mapping = await get_message_mapping_by_github_comment(comment_id, comment_type)
    if not mapping or mapping.origin != "github":
        return None
    db_pr = await get_pr_by_repo_and_number(payload["repository"]["full_name"], pr_number)
    if not db_pr:
        return None
    org = await get_org(db_pr.organization_id)
    return mapping, db_pr, (org.slack_bot_token if org else None)


async def handle_review_edited(payload: dict) -> None:
    review = payload["review"]
    body = review.get("body") or ""
    if SYNC_TAG in body:
        return

    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    resolved = await _mapping_and_token_for_github_comment(
        payload, review["id"], "review", pr["number"]
    )
    if not resolved:
        return
    mapping, _db_pr, token = resolved

    body_slack = await _github_body_to_slack(body) if body else ""
    message = _render_review_message(
        review["state"], review["html_url"], repo, pr["number"], body_slack
    )
    new_extra = await _resync_attributed_to_github_user(
        mapping.slack_channel_id,
        mapping.slack_ts,
        mapping.slack_ts_extra or [],
        message,
        review["user"]["login"],
        token,
    )
    await set_message_mapping_extra_ts(mapping.id, new_extra)
    logger.info("Synced GitHub review edit to Slack (review=%s)", review["id"])


async def handle_review_comment_edited(payload: dict) -> None:
    comment = payload["comment"]
    body = comment.get("body", "")
    if SYNC_TAG in body:
        return

    pr = payload["pull_request"]
    resolved = await _mapping_and_token_for_github_comment(
        payload, comment["id"], "review_comment", pr["number"]
    )
    if not resolved:
        return
    mapping, _db_pr, token = resolved

    body_slack = await _github_body_to_slack(body) if body else ""
    # A reply renders as the body alone; a thread root keeps its rich location header.
    if comment.get("in_reply_to_id"):
        message = body_slack or "replied"
    else:
        message = _render_review_comment_message(comment, body_slack)

    new_extra = await _resync_attributed_to_github_user(
        mapping.slack_channel_id,
        mapping.slack_ts,
        mapping.slack_ts_extra or [],
        message,
        comment["user"]["login"],
        token,
    )
    await set_message_mapping_extra_ts(mapping.id, new_extra)
    logger.info("Synced GitHub review-comment edit to Slack (%s)", comment["id"])


async def handle_review_comment_deleted(payload: dict) -> None:
    comment = payload["comment"]
    pr = payload["pull_request"]
    resolved = await _mapping_and_token_for_github_comment(
        payload, comment["id"], "review_comment", pr["number"]
    )
    if not resolved:
        return
    mapping, _db_pr, token = resolved

    for ts in [mapping.slack_ts, *(mapping.slack_ts_extra or [])]:
        await _delete_attributed_to_github_user(
            mapping.slack_channel_id, ts, comment["user"]["login"], token
        )
    await delete_message_mapping(mapping.id)
    logger.info("Synced GitHub review-comment deletion to Slack (%s)", comment["id"])


async def handle_issue_comment_edited(payload: dict) -> None:
    comment = payload["comment"]
    issue = payload["issue"]
    if "pull_request" not in issue:
        return

    body = comment.get("body", "")
    if SYNC_TAG in body:
        return

    resolved = await _mapping_and_token_for_github_comment(
        payload, comment["id"], "issue_comment", issue["number"]
    )
    if not resolved:
        return
    mapping, _db_pr, token = resolved

    repo = payload["repository"]["full_name"]
    body_slack = await _github_body_to_slack(body)
    message = _render_issue_comment_message(comment["html_url"], repo, issue["number"], body_slack)
    new_extra = await _resync_attributed_to_github_user(
        mapping.slack_channel_id,
        mapping.slack_ts,
        mapping.slack_ts_extra or [],
        message,
        comment["user"]["login"],
        token,
    )
    await set_message_mapping_extra_ts(mapping.id, new_extra)
    logger.info("Synced GitHub issue-comment edit to Slack (%s)", comment["id"])


async def handle_issue_comment_deleted(payload: dict) -> None:
    comment = payload["comment"]
    issue = payload["issue"]
    if "pull_request" not in issue:
        return

    resolved = await _mapping_and_token_for_github_comment(
        payload, comment["id"], "issue_comment", issue["number"]
    )
    if not resolved:
        return
    mapping, _db_pr, token = resolved

    for ts in [mapping.slack_ts, *(mapping.slack_ts_extra or [])]:
        await _delete_attributed_to_github_user(
            mapping.slack_channel_id, ts, comment["user"]["login"], token
        )
    await delete_message_mapping(mapping.id)
    logger.info("Synced GitHub issue-comment deletion to Slack (%s)", comment["id"])


# ── Slack → GitHub ────────────────────────────────────────


async def handle_slack_message(
    channel_id: str,
    slack_user_id: str,
    text: str,
    thread_ts: str | None,
    message_ts: str,
    team_id: str | None,
) -> None:
    if text.startswith(SLACK_SYNC_PREFIX):
        return

    db_pr = await get_pr_by_channel(channel_id)
    if not db_pr:
        return

    user = await get_user_by_slack_id(slack_user_id)

    org = await get_org(db_pr.organization_id)
    bot_token = org.slack_bot_token if org else None
    github_text = await _slack_text_to_github(text, bot_token)

    user_token = await get_valid_user_token(user) if user else None
    if user and user_token:
        github_body = f"{github_text}\n\n{SYNC_TAG}"

        # If this Slack message was sent as a reply in a thread that maps to
        # a GitHub review thread, reply there instead of creating a new
        # top-level comment.
        mapping = None
        if thread_ts:
            mapping = await get_thread_mapping_by_slack_ts(db_pr.id, thread_ts)

        if mapping:
            created = await reply_to_review_comment_as_user(
                user_token,
                db_pr.repo_full_name,
                db_pr.github_pr_number,
                int(mapping.github_thread_id),
                github_body,
            )
            comment_type = "review_comment"
            logger.info(
                "Synced Slack thread reply from %s to %s#%d (review thread %s, as user)",
                user.github_username,
                db_pr.repo_full_name,
                db_pr.github_pr_number,
                mapping.github_thread_id,
            )
        else:
            created = await create_issue_comment_as_user(
                user_token,
                db_pr.repo_full_name,
                db_pr.github_pr_number,
                github_body,
            )
            comment_type = "issue_comment"
            logger.info(
                "Synced Slack message from %s to %s#%d (as user)",
                user.github_username,
                db_pr.repo_full_name,
                db_pr.github_pr_number,
            )

        await _record_slack_origin_mapping(
            db_pr, channel_id, message_ts, slack_user_id, created, comment_type
        )
        return

    # Fallback: user hasn't linked GitHub — post bot-attributed with their
    # Slack display name so the message still reaches reviewers.
    if not org:
        return
    attribution = user.github_username if user else f"Slack user `{slack_user_id}`"
    github_body = f"**{attribution}** (via Slack):\n\n{github_text}\n\n{SYNC_TAG}"
    created = await create_issue_comment(
        org.github_installation_id,
        db_pr.repo_full_name,
        db_pr.github_pr_number,
        github_body,
    )
    await _record_slack_origin_mapping(
        db_pr, channel_id, message_ts, slack_user_id, created, "issue_comment"
    )
    logger.info(
        "Synced Slack message (as bot) for %s#%d — user not linked",
        db_pr.repo_full_name,
        db_pr.github_pr_number,
    )


async def _record_slack_origin_mapping(
    db_pr, channel_id: str, message_ts: str, slack_user_id: str, created: dict, comment_type: str
) -> None:
    """Record the Slack message ↔ GitHub comment link so later edits/deletes of
    the Slack message can be mirrored onto the GitHub comment.
    """
    comment_id = created.get("id")
    if comment_id is None:
        return
    await create_message_mapping(
        pull_request_id=db_pr.id,
        slack_channel_id=channel_id,
        slack_ts=message_ts,
        github_comment_id=comment_id,
        github_comment_type=comment_type,
        origin="slack",
        slack_user_id=slack_user_id,
    )


async def handle_slack_message_edited(
    channel_id: str,
    slack_user_id: str,
    text: str,
    message_ts: str,
) -> None:
    """Mirror an edit of a Slack message onto the GitHub comment it created."""
    if text.startswith(SLACK_SYNC_PREFIX):
        return

    db_pr = await get_pr_by_channel(channel_id)
    if not db_pr:
        return

    mapping = await get_message_mapping_by_slack_ts(db_pr.id, message_ts)
    if not mapping or mapping.origin != "slack":
        return

    org = await get_org(db_pr.organization_id)
    bot_token = org.slack_bot_token if org else None
    github_text = await _slack_text_to_github(text, bot_token)
    github_body = f"{github_text}\n\n{SYNC_TAG}"

    repo = db_pr.repo_full_name
    comment_id = mapping.github_comment_id

    # Mirror the create-time attribution: a comment posted as the user can only
    # be edited with their token; a bot-posted comment is edited as the bot.
    user = await get_user_by_slack_id(slack_user_id)
    user_token = await get_valid_user_token(user) if user else None

    if user_token:
        if mapping.github_comment_type == "review_comment":
            await update_review_comment_as_user(user_token, repo, comment_id, github_body)
        else:
            await update_issue_comment_as_user(user_token, repo, comment_id, github_body)
    elif org:
        if mapping.github_comment_type == "review_comment":
            await update_review_comment(org.github_installation_id, repo, comment_id, github_body)
        else:
            await update_issue_comment(org.github_installation_id, repo, comment_id, github_body)
    else:
        return

    logger.info(
        "Synced Slack edit to GitHub %s comment %s", mapping.github_comment_type, comment_id
    )


async def handle_slack_message_deleted(channel_id: str, message_ts: str) -> None:
    """Mirror a deletion of a Slack message onto the GitHub comment it created."""
    db_pr = await get_pr_by_channel(channel_id)
    if not db_pr:
        return

    mapping = await get_message_mapping_by_slack_ts(db_pr.id, message_ts)
    if not mapping or mapping.origin != "slack":
        return

    org = await get_org(db_pr.organization_id)
    repo = db_pr.repo_full_name
    comment_id = mapping.github_comment_id

    user = await get_user_by_slack_id(mapping.slack_user_id) if mapping.slack_user_id else None
    user_token = await get_valid_user_token(user) if user else None

    if user_token:
        if mapping.github_comment_type == "review_comment":
            await delete_review_comment_as_user(user_token, repo, comment_id)
        else:
            await delete_issue_comment_as_user(user_token, repo, comment_id)
    elif org:
        if mapping.github_comment_type == "review_comment":
            await delete_review_comment(org.github_installation_id, repo, comment_id)
        else:
            await delete_issue_comment(org.github_installation_id, repo, comment_id)
    else:
        return

    await delete_message_mapping(mapping.id)
    logger.info(
        "Synced Slack deletion to GitHub %s comment %s", mapping.github_comment_type, comment_id
    )


# ── Helpers ───────────────────────────────────────────────


async def _slack_text_to_github(text: str, bot_token: str | None) -> str:
    """Render Slack mrkdwn as GitHub Markdown, resolving Slack @mentions: a user
    linked to GitHub becomes their `@login` (a real GitHub ping); an unlinked
    user becomes their Slack display name as plain text (no false ping).
    """
    ids = slack_mention_ids(text)
    if not ids:
        return slack_to_gfm(text)

    github_by_slack = await get_github_username_map_for_slack_ids(ids)
    replacements: dict[str, str] = {}
    for slack_id in ids:
        if slack_id in github_by_slack:
            replacements[slack_id] = f"@{github_by_slack[slack_id]}"
        else:
            name = await _slack_display_name(slack_id, bot_token)
            if name:
                replacements[slack_id] = name
    return slack_to_gfm(text, replacements)


async def _slack_display_name(slack_user_id: str, bot_token: str | None) -> str | None:
    """Display name (or real name) of a Slack user; None if it can't be fetched."""
    try:
        info = await slack_service.get_user_info(slack_user_id, token=bot_token)
    except slack_sdk.errors.SlackApiError:
        return None
    profile = info.get("profile", {})
    return profile.get("display_name") or profile.get("real_name") or None


async def _slack_identity_for_github(
    github_username: str, bot_token: str | None
) -> tuple[str | None, str | None]:
    """Display name + avatar URL of the linked Slack user, for the bot's
    chat:write.customize fallback. Both None if the user isn't linked.
    """
    slack_ids = await get_slack_ids_for_github_usernames([github_username])
    if not slack_ids:
        return None, None
    try:
        info = await slack_service.get_user_info(slack_ids[0], token=bot_token)
    except slack_sdk.errors.SlackApiError:
        return None, None
    profile = info.get("profile", {})
    name = profile.get("display_name") or profile.get("real_name") or github_username
    icon = profile.get("image_72") or profile.get("image_48")
    return name, icon


async def _post_attributed_to_github_user(
    channel_id: str,
    text: str,
    github_username: str,
    bot_token: str | None,
    *,
    thread_ts: str | None = None,
) -> list[str]:
    """Post a GitHub-originated message into Slack with the most accurate
    attribution available, returning the ts of every message it posted (the
    parent first, then any threaded continuations for an over-long body).

    1. If the linked Slack user has granted chat:write user-scope (per-user
       OAuth), post with their token — Slack natively renders it as them.
    2. Otherwise fall back to bot + chat:write.customize (display_name + avatar
       impersonation). Less faithful but works without per-user OAuth.
    3. If the user isn't linked at all, post as the bare bot.
    """
    user = await get_user_by_github_username(github_username)

    if user and user.slack_user_token:
        try:
            sent = await slack_service.post_chunked_message(
                channel_id,
                text,
                thread_ts=thread_ts,
                token=user.slack_user_token,
            )
            return [m["ts"] for m in sent if m.get("ts")]
        except slack_sdk.errors.SlackApiError as e:
            # User token may have been revoked, scope changed, or user isn't a
            # member of the channel. Fall through to the bot fallback.
            logger.warning(
                "Post-as-user failed for %s (%s); falling back to bot impersonation",
                github_username,
                e.response.get("error"),
            )

    username, icon_url = await _slack_identity_for_github(github_username, bot_token)
    sent = await slack_service.post_chunked_message(
        channel_id,
        text,
        thread_ts=thread_ts,
        username=username,
        icon_url=icon_url,
        token=bot_token,
    )
    return [m["ts"] for m in sent if m.get("ts")]


async def _update_attributed_to_github_user(
    channel_id: str,
    ts: str,
    text: str,
    github_username: str,
    bot_token: str | None,
) -> dict:
    """Edit a previously-synced GitHub→Slack message, using the token that posted
    it: the user's own token if they posted as themselves, else the bot. Mirrors
    _post_attributed_to_github_user's resolution.
    """
    user = await get_user_by_github_username(github_username)

    if user and user.slack_user_token:
        try:
            return await slack_service.update_message(
                channel_id, ts, text=text, token=user.slack_user_token
            )
        except slack_sdk.errors.SlackApiError as e:
            logger.warning(
                "Update-as-user failed for %s (%s); falling back to bot",
                github_username,
                e.response.get("error"),
            )

    return await slack_service.update_message(channel_id, ts, text=text, token=bot_token)


async def _delete_attributed_to_github_user(
    channel_id: str,
    ts: str,
    github_username: str,
    bot_token: str | None,
) -> dict:
    """Delete a previously-synced GitHub→Slack message with the token that posted it."""
    user = await get_user_by_github_username(github_username)

    if user and user.slack_user_token:
        try:
            return await slack_service.delete_message(channel_id, ts, token=user.slack_user_token)
        except slack_sdk.errors.SlackApiError as e:
            logger.warning(
                "Delete-as-user failed for %s (%s); falling back to bot",
                github_username,
                e.response.get("error"),
            )

    return await slack_service.delete_message(channel_id, ts, token=bot_token)


async def _resync_attributed_to_github_user(
    channel_id: str,
    parent_ts: str,
    extra_ts: list[str],
    text: str,
    github_username: str,
    bot_token: str | None,
) -> list[str]:
    """Mirror an edited GitHub comment onto its Slack message(s).

    A long comment spans a parent message plus threaded continuations. We
    re-split the new body and reconcile: update each message that still has a
    part at its position, post new continuations (threaded under the parent) for
    added parts, and delete the messages whose parts went away. The parent ts is
    preserved so thread mappings stay valid. Returns the new continuation ts list.
    """
    ts_list = [parent_ts, *extra_ts]
    parts = split_for_slack(text) if len(text) > SLACK_TEXT_LIMIT else [text]

    for ts, part in zip(ts_list, parts, strict=False):
        await _update_attributed_to_github_user(channel_id, ts, part, github_username, bot_token)

    new_extra = list(extra_ts[: max(0, len(parts) - 1)])
    for part in parts[len(ts_list) :]:
        new_extra += await _post_attributed_to_github_user(
            channel_id, part, github_username, bot_token, thread_ts=parent_ts
        )

    for ts in ts_list[len(parts) :]:
        await _delete_attributed_to_github_user(channel_id, ts, github_username, bot_token)

    return new_extra
