"""Channel manager — creates/archives ephemeral Slack channels per pull request."""

import fnmatch
import logging
import re

import slack_sdk.errors

from src.config import settings
from src.db.queries import (
    close_pr,
    count_open_prs_in_channel,
    create_pr_and_channel,
    get_org,
    get_org_by_installation,
    get_pr_by_github_id,
    get_pr_by_repo_and_number,
    get_slack_ids_for_github_usernames,
    get_stack_parent,
    get_user_by_slack_id,
    get_user_with_timeslots,
    reopen_pr,
    set_channel_archived,
    set_pr_base_branch,
    set_pr_draft_state,
    set_pr_reviewers,
    set_pr_title,
    upsert_organization,
)
from src.services import pr_digest_service, slack_service
from src.services.channel_target import resolve_channel_pr
from src.services.github_service import github_request
from src.services.timeslot_service import is_user_available

logger = logging.getLogger(__name__)

_CHANNEL_NAME_RE = re.compile(r"[^a-z0-9_-]")
MAX_CHANNEL_NAME = 80


def _make_channel_name(repo: str, pr_number: int) -> str:
    short_repo = repo.split("/")[-1].lower()
    # Leading underscore so PR channels sort to the top of the channel list.
    name = f"_pr-{short_repo}-{pr_number}"
    name = _CHANNEL_NAME_RE.sub("-", name)
    return name[:MAX_CHANNEL_NAME]


async def _get_or_backfill_org(payload: dict, installation_id: int):
    """Self-heal: if we missed the installation.created event, create the org
    row on the fly from the webhook payload. The repository.owner block on a
    PR webhook carries everything we need for the GitHub-side fields.
    """
    org = await get_org_by_installation(installation_id)
    if org:
        return org
    owner = payload["repository"]["owner"]
    logger.info(
        "Backfilling org from webhook: login=%s installation=%d",
        owner["login"],
        installation_id,
    )
    return await upsert_organization(
        github_org_id=owner["id"],
        github_org_login=owner["login"],
        github_installation_id=installation_id,
    )


def _should_skip_channel(payload: dict) -> bool:
    pr = payload["pull_request"]
    body = (pr.get("body") or "").lower()
    if "_noslackchannel" in body:
        return True
    labels = [label["name"].lower() for label in pr.get("labels", [])]
    return "_noslackchannel" in labels


def _channel_label() -> str:
    """The configured channel label, normalised. Empty means no label gate."""
    return settings.slack_channel_label.strip().lower()


def _has_channel_label(payload: dict) -> bool:
    """True when no channel label is configured, or the PR carries it."""
    wanted = _channel_label()
    if not wanted:
        return True
    labels = [label["name"].lower() for label in payload["pull_request"].get("labels", [])]
    return wanted in labels


async def _post(db_pr, text: str, token: str | None) -> dict:
    """Post about a PR: top-level in its own channel, or in its thread when it
    is a stacked PR sharing its stack's channel."""
    return await slack_service.post_message(
        db_pr.slack_channel_id, text, thread_ts=db_pr.slack_thread_ts, token=token
    )


# ── Event handlers ────────────────────────────────────────


async def handle_pr_opened(payload: dict) -> None:
    if _channel_label():
        # The channel opens on the `labeled` event instead. GitHub sends one
        # for a label set at creation too, so acting here as well would race it.
        logger.info("Channel label configured; waiting for it before opening a channel")
        return
    await _open_channel(payload)


async def handle_pr_labeled(payload: dict) -> None:
    """Open the PR's channel when the configured channel label is added."""
    wanted = _channel_label()
    if not wanted or payload["label"]["name"].lower() != wanted:
        return
    if payload["pull_request"].get("state") != "open":
        return
    await _open_channel(payload)


async def _open_channel(payload: dict) -> None:
    if _should_skip_channel(payload):
        logger.info("Skipping channel creation (opt-out marker found)")
        return
    if not _has_channel_label(payload):
        logger.info("Skipping channel creation (channel label missing)")
        return

    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]
    installation_id = payload["installation"]["id"]

    # Idempotency: GitHub redelivers `opened` (manually, or after we 500 on a
    # transient mid-flow error). A tracked PR means the open flow already
    # completed — don't create a second channel or DB row.
    if await get_pr_by_github_id(pr["id"]):
        logger.info("PR %s#%d already tracked; skipping open flow", repo, pr_number)
        return

    org = await _get_or_backfill_org(payload, installation_id)
    if not org.slack_bot_token:
        logger.warning(
            "Org %s has no Slack link; skipping channel creation "
            "(complete /auth/slack or the App Home link flow to enable)",
            org.github_org_login,
        )
        return

    if settings.stack_channels and _can_stack_on(payload, pr["base"]["ref"]):
        parent = await get_stack_parent(repo, pr["base"]["ref"])
        if parent:
            await _join_stack(payload, org, parent)
            return

    channel_name = _make_channel_name(repo, pr_number)
    logger.info("Creating channel %s for %s#%d", channel_name, repo, pr_number)

    token = org.slack_bot_token
    try:
        channel = await slack_service.create_channel(channel_name, token=token)
    except slack_sdk.errors.SlackApiError as e:
        # A prior delivery created the channel but crashed before persisting the
        # PR row (the guard above only catches fully-tracked PRs). Resume on the
        # existing channel rather than failing the redelivery.
        if e.response.get("error") != "name_taken":
            raise
        channel = await slack_service.find_channel_by_name(channel_name, token=token)
        if channel is None:
            raise
        logger.info("Channel %s already exists; resuming open flow", channel_name)
    channel_id = channel["id"]

    await slack_service.set_channel_topic(
        channel_id,
        f"PR: {pr['html_url']} | {pr['title']}",
        token=token,
    )

    title_bookmark = await slack_service.add_bookmark(
        channel_id,
        title=f"#{pr_number} {pr['title']}",
        link=pr["html_url"],
        token=token,
    )

    author = pr["user"]["login"]
    state_emoji = "📝" if pr.get("draft") else "🟢"
    await slack_service.post_message(
        channel_id,
        f"{state_emoji} *<{pr['html_url']}|{repo}#{pr_number}>* opened by *{author}*\n"
        f"> {pr['title']}\n"
        f"Base: `{pr['base']['ref']}` ← Head: `{pr['head']['ref']}`",
        token=token,
    )

    reviewers = [r["login"] for r in pr.get("requested_reviewers", [])]
    all_users = [author] + reviewers
    slack_ids = await get_slack_ids_for_github_usernames(all_users)
    if slack_ids:
        await slack_service.invite_to_channel(channel_id, slack_ids, token=token)

    db_pr = await create_pr_and_channel(
        organization_id=org.id,
        github_pr_id=pr["id"],
        github_pr_number=pr_number,
        repo_full_name=repo,
        title=pr["title"],
        is_draft=pr.get("draft", False),
        head_branch=pr["head"]["ref"],
        base_branch=pr["base"]["ref"],
        html_url=pr["html_url"],
        author_github_id=pr["user"]["id"],
        author_github_username=author,
        slack_channel_id=channel_id,
        slack_channel_name=channel_name,
        title_bookmark_id=title_bookmark["id"],
        head_repo_full_name=_head_repo(pr),
    )
    if reviewers:
        db_pr = await set_pr_reviewers(pr["id"], reviewers)

    # Org-wide digest (one persistent message per PR, updated in place)

    await pr_digest_service.post_initial(db_pr, org)

    logger.info(
        "Channel %s (%s) created for %s#%d",
        channel_name,
        channel_id,
        repo,
        pr_number,
    )


def _head_repo(pr: dict) -> str | None:
    # `head.repo` is null when the fork the PR came from was deleted.
    return ((pr.get("head") or {}).get("repo") or {}).get("full_name")


def _can_stack_on(payload: dict, base_branch: str) -> bool:
    """False for a base branch that is long-lived rather than another PR's
    feature branch: the default branch, or one in STACK_PARENT_EXCLUDED_BRANCHES.
    A long-lived PR such as develop -> main would otherwise collect every PR
    that targets develop."""
    if base_branch == payload["repository"].get("default_branch"):
        return False
    patterns = [p.strip() for p in settings.stack_parent_excluded_branches.split(",")]
    return not any(p and fnmatch.fnmatchcase(base_branch, p) for p in patterns)


async def _join_stack(payload: dict, org, parent) -> None:
    """Track a stacked PR in its stack's channel: one top-level parent message,
    and everything else about the PR in that message's thread."""
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    channel_id = parent.slack_channel_id
    token = org.slack_bot_token
    author = pr["user"]["login"]
    state_emoji = "📝" if pr.get("draft") else "🟢"

    try:
        await slack_service.unarchive_channel(channel_id, token=token)
    except Exception as e:  # noqa: BLE001 — Slack raises `not_archived` if already active
        logger.debug("Unarchive skipped for %s: %s", channel_id, e)

    parent_msg = await slack_service.post_message(
        channel_id,
        f"{state_emoji} *<{pr['html_url']}|{repo}#{pr['number']}>* opened by *{author}*, "
        f"stacked on #{parent.github_pr_number}\n"
        f"> {pr['title']}\n"
        f"Base: `{pr['base']['ref']}` ← Head: `{pr['head']['ref']}`\n"
        "_Updates on this PR go in this thread._",
        token=token,
    )

    # The DB row is the open flow's idempotency check, so write it straight
    # after the parent message. If the write fails, remove the message, so a
    # redelivery does not leave an orphaned parent behind.
    try:
        db_pr = await create_pr_and_channel(
            organization_id=org.id,
            github_pr_id=pr["id"],
            github_pr_number=pr["number"],
            repo_full_name=repo,
            title=pr["title"],
            is_draft=pr.get("draft", False),
            head_branch=pr["head"]["ref"],
            base_branch=pr["base"]["ref"],
            html_url=pr["html_url"],
            author_github_id=pr["user"]["id"],
            author_github_username=author,
            slack_channel_id=channel_id,
            slack_channel_name="",
            slack_thread_ts=parent_msg["ts"],
            head_repo_full_name=_head_repo(pr),
        )
    except Exception:
        try:
            await slack_service.delete_message(channel_id, parent_msg["ts"], token=token)
        except slack_sdk.errors.SlackApiError:
            logger.warning("Could not remove orphaned parent message %s", parent_msg["ts"])
        raise

    reviewers = [r["login"] for r in pr.get("requested_reviewers", [])]
    slack_ids = await get_slack_ids_for_github_usernames([author] + reviewers)
    if slack_ids:
        await slack_service.invite_to_channel(channel_id, slack_ids, token=token)

    if reviewers:
        db_pr = await set_pr_reviewers(pr["id"], reviewers)

    await pr_digest_service.post_initial(db_pr, org)

    logger.info(
        "%s#%d stacked on #%d; joined channel %s",
        repo,
        pr["number"],
        parent.github_pr_number,
        channel_id,
    )


async def handle_pr_closed(payload: dict) -> None:
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]
    merged = pr.get("merged", False)

    db_pr = await close_pr(pr["id"], merged=merged)
    if not db_pr or not db_pr.slack_channel_id:
        logger.info("PR %s#%d closed but no channel tracked", repo, pr_number)
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None
    channel_id = db_pr.slack_channel_id

    action = "merged" if merged else "closed"
    emoji = "✅" if merged else "❌"
    await _post(db_pr, f"PR {action} {emoji}", token)
    # A stack's channel stays open while any PR in it is still open.
    remaining = await count_open_prs_in_channel(channel_id)
    if not remaining:
        await slack_service.archive_channel(channel_id, token=token)
        await set_channel_archived(channel_id, True)

    if org:
        await pr_digest_service.update(pr["id"], org)

    logger.info(
        "PR %s#%d %s — channel %s %s",
        repo,
        pr_number,
        action,
        channel_id,
        f"kept for {remaining} open PR(s)" if remaining else "archived",
    )


async def handle_pr_reopened(payload: dict) -> None:
    """Reopen the channel for a previously-closed PR, or run the full open
    flow if the PR was never tracked (e.g., opened before Pulley was installed).
    """
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]

    existing = await get_pr_by_github_id(pr["id"])
    if not existing:
        logger.info("PR %s#%d reopened but not tracked — running open flow", repo, pr_number)
        # `_open_channel` (not handle_pr_opened) so a reopened PR that already
        # carries the channel label gets its channel without a fresh `labeled`.
        await _open_channel(payload)
        return

    db_pr = await reopen_pr(pr["id"])
    if not db_pr or not db_pr.slack_channel_id:
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None
    channel_id = db_pr.slack_channel_id

    # Channel may or may not be archived depending on close history.
    try:
        await slack_service.unarchive_channel(channel_id, token=token)
    except Exception as e:  # noqa: BLE001 — Slack raises `not_archived` if already active
        logger.debug("Unarchive skipped for %s: %s", channel_id, e)
    await set_channel_archived(channel_id, False)

    await _post(db_pr, "🔄 PR reopened", token)

    if org:
        await pr_digest_service.update(pr["id"], org)

    logger.info("PR %s#%d reopened — channel %s active again", repo, pr_number, channel_id)


async def handle_pr_updated(payload: dict, action: str) -> None:
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]

    db_pr = await get_pr_by_repo_and_number(repo, pr_number)
    if not db_pr or not db_pr.slack_channel_id:
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None

    # `edited` fires for title, body, or base-branch changes alike; the `changes`
    # object says which. We act on renames and merge-target changes; a body edit
    # is a no-op.
    if action == "edited":
        changes = payload.get("changes", {})
        if "title" in changes:
            await _handle_pr_renamed(db_pr, pr, org, token)
        if "base" in changes:
            await _handle_pr_base_changed(db_pr, pr, changes["base"], org, token)
        return

    messages = {
        "converted_to_draft": "📝 PR converted to draft",
        "ready_for_review": "🟢 PR is ready for review",
        "synchronize": f"🔄 New commits pushed to `{pr['head']['ref']}`",
    }

    msg = messages.get(action, f"PR updated: {action}")
    await _post(db_pr, msg, token)

    # Track draft state changes on the DB row so the digest renders correctly
    if action == "converted_to_draft":
        await set_pr_draft_state(pr["id"], True)
    elif action == "ready_for_review":
        await set_pr_draft_state(pr["id"], False)

    if org and action in ("converted_to_draft", "ready_for_review"):
        await pr_digest_service.update(pr["id"], org)

    logger.info("%s#%d: %s", repo, pr_number, msg)


async def _handle_pr_renamed(db_pr, pr: dict, org, token: str | None) -> None:
    """Propagate a PR title change to every Slack surface that shows it: the DB
    row (which the digest and `/pulley` listings read from), the channel topic,
    the channel's PR bookmark, and a one-line notice in the channel.
    """
    new_title = pr["title"]
    channel_id = db_pr.slack_channel_id

    await set_pr_title(pr["id"], new_title)

    if db_pr.slack_thread_ts:
        # A stacked PR does not own the channel's topic or bookmarks.
        await _post(db_pr, f"✏️ PR title updated: *{new_title}*", token)
        if org:
            await pr_digest_service.update(pr["id"], org)
        return

    await slack_service.set_channel_topic(
        channel_id,
        f"PR: {pr['html_url']} | {new_title}",
        token=token,
    )

    if db_pr.title_bookmark_id:
        try:
            await slack_service.edit_bookmark(
                channel_id,
                db_pr.title_bookmark_id,
                title=f"#{db_pr.github_pr_number} {new_title}",
                token=token,
            )
        except slack_sdk.errors.SlackApiError as e:
            # Stale id (bookmark removed by hand, channel gone) — log, don't fail.
            logger.warning(
                "Failed to update title bookmark for %s: %s",
                channel_id,
                e.response.get("error"),
            )

    await slack_service.post_message(
        channel_id,
        f"✏️ PR title updated: *{new_title}*",
        token=token,
    )

    if org:
        await pr_digest_service.update(pr["id"], org)

    logger.info("%s#%d renamed to %r", db_pr.repo_full_name, db_pr.github_pr_number, new_title)


async def _handle_pr_base_changed(
    db_pr, pr: dict, base_change: dict, org, token: str | None
) -> None:
    """Propagate a merge-target (base branch) change to the DB row (which the
    digest reads from), a one-line notice in the channel, and the digest.
    """
    new_base = pr["base"]["ref"]
    old_base = base_change["ref"]["from"]

    await set_pr_base_branch(pr["id"], new_base)

    await _post(db_pr, f"🎯 Merge target changed: `{old_base}` → `{new_base}`", token)

    if org:
        await pr_digest_service.update(pr["id"], org)

    logger.info(
        "%s#%d base changed %r → %r",
        db_pr.repo_full_name,
        db_pr.github_pr_number,
        old_base,
        new_base,
    )


async def handle_reviewer_requested(payload: dict) -> None:
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]
    requested = payload.get("requested_reviewer", {})

    if not requested:
        return

    github_username = requested["login"]
    logger.info("Reviewer %s requested on %s#%d", github_username, repo, pr_number)

    db_pr = await get_pr_by_repo_and_number(repo, pr_number)
    if not db_pr or not db_pr.slack_channel_id:
        return

    org = await get_org(db_pr.organization_id)
    token = org.slack_bot_token if org else None

    # Sync reviewers list from the (now-updated) webhook payload, update digest
    current_reviewers = [r["login"] for r in pr.get("requested_reviewers", [])]
    await set_pr_reviewers(pr["id"], current_reviewers)
    if org:
        await pr_digest_service.update(pr["id"], org)

    slack_ids = await get_slack_ids_for_github_usernames([github_username])
    if slack_ids:
        await slack_service.invite_to_channel(db_pr.slack_channel_id, slack_ids, token=token)

        # Check reviewer's time slot availability

        reviewer = await get_user_with_timeslots(slack_ids[0])
        slots = [
            {
                "day_of_week": s.day_of_week,
                "start_time": s.start_time,
                "end_time": s.end_time,
            }
            for s in (reviewer.review_time_slots if reviewer else [])
        ]
        tz = slots[0].get("timezone", "UTC") if slots else "UTC"

        if is_user_available(slots, tz):
            await _post(db_pr, f"🔍 *{github_username}* was requested for review", token)
        else:
            await _post(
                db_pr,
                f"🔍 *{github_username}* was requested for review "
                f"(outside review hours — they'll be notified later)",
                token,
            )
            logger.info(
                "Reviewer %s is outside review hours, notification deferred",
                github_username,
            )


async def handle_reviewer_removed(payload: dict) -> None:
    """Sync reviewers list and digest when a reviewer is removed from a PR.

    We do NOT kick the removed user from the Slack channel — they may still
    want to follow the discussion.
    """
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]
    removed = payload.get("requested_reviewer", {})
    if not removed:
        return

    db_pr = await get_pr_by_repo_and_number(repo, pr_number)
    if not db_pr:
        return

    current_reviewers = [r["login"] for r in pr.get("requested_reviewers", [])]
    await set_pr_reviewers(pr["id"], current_reviewers)

    org = await get_org(db_pr.organization_id)
    if org:
        await pr_digest_service.update(pr["id"], org)

    logger.info(
        "Reviewer %s removed from %s#%d",
        removed.get("login"),
        repo,
        pr_number,
    )


async def handle_reaction_reviewer(
    channel_id: str, slack_user_id: str, team_id: str | None, message_ts: str | None = None
) -> None:
    """Self-assign as PR reviewer when 🔍 reaction is added in a PR channel.

    In a stack's channel the reacted message picks the PR: a stacked PR's
    parent message, or a message mapped to one of its comments. Any other
    message counts for the owner while it is open, else the only open PR.
    """
    db_pr = (await resolve_channel_pr(channel_id, message_ts=message_ts)).pr
    if not db_pr or db_pr.state != "open":
        return

    user = await get_user_by_slack_id(slack_user_id)
    if not user:
        return

    org = await get_org(db_pr.organization_id)
    if not org:
        return

    await github_request(
        "POST",
        f"/repos/{db_pr.repo_full_name}/pulls/{db_pr.github_pr_number}/requested_reviewers",
        org.github_installation_id,
        json={"reviewers": [user.github_username]},
    )

    await _post(
        db_pr, f"🔍 *{user.github_username}* self-assigned as reviewer", org.slack_bot_token
    )
    logger.info(
        "User %s self-assigned as reviewer on %s#%d",
        user.github_username,
        db_pr.repo_full_name,
        db_pr.github_pr_number,
    )
