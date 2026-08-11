"""Slash command handlers for /lgtm and /pulley (open, me, team, merge)."""

import logging

import httpx

from src.db.queries import (
    get_open_prs_for_author,
    get_open_prs_for_org,
    get_org,
    get_pr_by_channel,
    get_user_by_slack_id,
)
from src.services import slack_service
from src.services.github_service import (
    create_review_as_user,
    get_valid_user_token,
    merge_pull_request,
)

logger = logging.getLogger(__name__)


async def handle_lgtm(
    channel_id: str,
    slack_user_id: str,
    team_id: str,
    comment: str,
) -> dict:
    """/lgtm — approve the PR associated with this channel."""
    db_pr = await get_pr_by_channel(channel_id)
    if not db_pr:
        return {"response_type": "ephemeral", "text": "This channel isn't linked to a PR."}

    user = await get_user_by_slack_id(slack_user_id)
    if not user:
        return {"response_type": "ephemeral", "text": "Link your GitHub account first."}

    org = await get_org(db_pr.organization_id)
    if not org:
        return {"response_type": "ephemeral", "text": "Organization not found."}

    user_token = await get_valid_user_token(user)
    if not user_token:
        return {
            "response_type": "ephemeral",
            "text": "Your GitHub link has expired — re-link via the Pulley app home and try again.",
        }

    try:
        await create_review_as_user(
            user_token,
            db_pr.repo_full_name,
            db_pr.github_pr_number,
            event="APPROVE",
            body=comment,
        )
    except httpx.HTTPStatusError as exc:
        # GitHub returns 422 when a user tries to approve their own PR.
        if exc.response.status_code == 422:
            return {
                "response_type": "ephemeral",
                "text": "GitHub rejected the approval (you can't approve your own PR).",
            }
        raise

    await slack_service.post_message(
        channel_id,
        f"✅ *{user.github_username}* approved this PR",
        token=org.slack_bot_token,
    )

    logger.info(
        "LGTM from %s on %s#%d",
        user.github_username,
        db_pr.repo_full_name,
        db_pr.github_pr_number,
    )
    return {"response_type": "in_channel", "text": f"✅ <@{slack_user_id}> approved this PR"}


async def handle_pulley_command(
    subcommand: str,
    channel_id: str,
    slack_user_id: str,
    team_id: str,
) -> dict:
    parts = subcommand.split(maxsplit=1)
    cmd = parts[0].lower() if parts else "help"
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "open":
        return await _cmd_open(team_id)
    elif cmd == "me":
        return await _cmd_me(slack_user_id)
    elif cmd == "team":
        return await _cmd_team(arg, team_id)
    elif cmd == "merge":
        return await _cmd_merge(channel_id, slack_user_id, arg)
    elif cmd == "settings":
        return await _cmd_settings(arg, team_id, channel_id)
    else:
        return _cmd_help()


async def _cmd_open(team_id: str) -> dict:
    prs = await get_open_prs_for_org(team_id)
    if not prs:
        return {"response_type": "ephemeral", "text": "No open pull requests."}

    lines = [f"*Open pull requests ({len(prs)}):*"]
    for pr in prs:
        lines.append(
            f"  • <{pr.html_url}|#{pr.github_pr_number}> {pr.title} — {pr.author_github_username}"
        )
    return {"response_type": "ephemeral", "text": "\n".join(lines)}


async def _cmd_me(slack_user_id: str) -> dict:
    user = await get_user_by_slack_id(slack_user_id)
    if not user:
        return {
            "response_type": "ephemeral",
            "text": "Link your GitHub account first via /auth/github",
        }

    prs = await get_open_prs_for_author(user.github_user_id)
    if not prs:
        return {"response_type": "ephemeral", "text": "You have no open pull requests."}

    lines = [f"*Your open pull requests ({len(prs)}):*"]
    for pr in prs:
        lines.append(f"  • <{pr.html_url}|#{pr.github_pr_number}> {pr.title}")
    return {"response_type": "ephemeral", "text": "\n".join(lines)}


async def _cmd_team(team_name: str, team_id: str) -> dict:
    if not team_name:
        return {"response_type": "ephemeral", "text": "Usage: `/pulley team <team-name>`"}

    from src.db.queries import get_org_by_slack_team
    from src.services.github_service import get_team_members

    org = await get_org_by_slack_team(team_id)
    if not org:
        return {"response_type": "ephemeral", "text": "Organization not connected."}

    try:
        members = await get_team_members(
            org.github_installation_id, org.github_org_login, team_name
        )
    except httpx.HTTPStatusError:
        logger.exception("Failed to fetch team members for %s", team_name)
        return {
            "response_type": "ephemeral",
            "text": f"Could not find team `{team_name}`. Check the team slug.",
        }

    member_ids = {m["id"] for m in members}
    prs = await get_open_prs_for_org(team_id)
    team_prs = [pr for pr in prs if pr.author_github_id in member_ids]

    if not team_prs:
        return {
            "response_type": "ephemeral",
            "text": f"No open PRs for team *{team_name}*.",
        }

    lines = [f"*Open PRs for team {team_name} ({len(team_prs)}):*"]
    for pr in team_prs:
        lines.append(
            f"  • <{pr.html_url}|#{pr.github_pr_number}> {pr.title} — {pr.author_github_username}"
        )
    return {"response_type": "ephemeral", "text": "\n".join(lines)}


async def _cmd_merge(channel_id: str, slack_user_id: str, method: str) -> dict:
    merge_method = method.strip().lower() if method else "merge"
    if merge_method not in ("merge", "squash", "rebase"):
        merge_method = "merge"

    db_pr = await get_pr_by_channel(channel_id)
    if not db_pr:
        return {"response_type": "ephemeral", "text": "This channel isn't linked to a PR."}

    org = await get_org(db_pr.organization_id)
    if not org:
        return {"response_type": "ephemeral", "text": "Organization not found."}

    await merge_pull_request(
        org.github_installation_id,
        db_pr.repo_full_name,
        db_pr.github_pr_number,
        merge_method,
    )

    logger.info(
        "/pulley merge (%s) on %s#%d",
        merge_method,
        db_pr.repo_full_name,
        db_pr.github_pr_number,
    )
    return {
        "response_type": "in_channel",
        "text": f"🔀 <@{slack_user_id}> merged this PR ({merge_method})",
    }


# `/pulley settings <name>` → the organizations column it writes, and its label.
_SETTINGS: dict[str, tuple[str, str]] = {
    "pr": ("pr_channel_id", "PR digest"),
    "ci": ("ci_channel_id", "CI alerts"),
    "recap": ("recap_channel_id", "Daily recap"),
}


def _parse_channel_id(raw: str) -> str | None:
    """Extract the channel ID from Slack's escaped channel mention.

    `should_escape` is on for /pulley, so a channel the user picked from Slack's
    autocomplete arrives as a mention rather than plain text: `<#C123|name>` for
    a public channel, and the ID alone for a private one.

    Returns None for anything else. Text Slack declined to linkify isn't a
    channel we can post to, and storing it would leave the setting looking
    configured while every later post failed.
    """
    if not (raw.startswith("<") and raw.endswith(">")):
        return None
    cid = raw[1:-1].lstrip("#").split("|", 1)[0]
    return cid or None


def _blurb(name: str, org) -> str:
    """What Pulley posts to the channel behind `name`, phrased to follow "Pulley posts"."""
    if name == "pr":
        return "one message per open PR, updated in place as reviews land"
    if name == "ci":
        return "failed checks and deployment statuses on the default branch"

    from src.config import settings as app_settings

    cron = org.recap_cron or app_settings.recap_cron
    return f"a summary of open PRs, on the `{cron}` schedule (UTC)"


def _settings_overview(org) -> str:
    if org.github_installation_id and org.github_org_login:
        gh_status = f"✅ Linked to *{org.github_org_login}*"
    else:
        gh_status = (
            "⚠️ Not linked — open the Pulley App Home and click "
            "*Link organization* to connect a GitHub org"
        )

    lines = ["*Pulley settings*", f"GitHub: {gh_status}", "", "*Channels*"]
    for name, (column, label) in _SETTINGS.items():
        target = f"<#{getattr(org, column)}>" if getattr(org, column) else "_not set_"
        lines.append(f"• *{label}* — `{name}` → {target}")
        lines.append(f"     Pulley posts {_blurb(name, org)}.")

    lines += [
        "",
        "*Usage*",
        "• `/pulley settings <name> #channel` — send that kind of message to #channel",
        "• `/pulley settings <name>` — show just that one",
        "",
        "A setting with no channel is off — Pulley posts nothing for it. "
        "For a private channel, invite Pulley to it first.",
    ]
    return "\n".join(lines)


async def _cmd_settings(arg: str, team_id: str, channel_id: str) -> dict:
    from src.db.queries import get_org_by_slack_team, update_org_settings

    org = await get_org_by_slack_team(team_id)
    if not org:
        return {"response_type": "ephemeral", "text": "Organization not connected."}

    parts = arg.split(maxsplit=1)
    name = parts[0].lower() if parts else ""
    value = parts[1].strip() if len(parts) > 1 else ""

    if not name:
        return {"response_type": "ephemeral", "text": _settings_overview(org)}

    if name not in _SETTINGS:
        valid = ", ".join(f"`{k}`" for k in _SETTINGS)
        return {
            "response_type": "ephemeral",
            "text": (
                f"Unknown setting `{name}`. Valid settings: {valid}.\n"
                "Run `/pulley settings` to see what each one does."
            ),
        }

    column, label = _SETTINGS[name]

    if not value:
        current = getattr(org, column)
        if not current:
            return {
                "response_type": "ephemeral",
                "text": (
                    f"*{label}* — `{name}` → _not set_, so Pulley posts nothing for it.\n"
                    f"Run `/pulley settings {name} #channel` and that channel will get: "
                    f"{_blurb(name, org)}."
                ),
            }
        return {
            "response_type": "ephemeral",
            "text": f"*{label}* — `{name}` → <#{current}>\nPulley posts {_blurb(name, org)}.",
        }

    cid = _parse_channel_id(value)
    if not cid:
        return {
            "response_type": "ephemeral",
            "text": (
                f"Couldn't read a channel from `{value}`. Pick the channel from Slack's "
                f"autocomplete as you type, so it arrives as a link — "
                f"`/pulley settings {name} #channel`."
            ),
        }

    await update_org_settings(org.id, **{column: cid})
    return {
        "response_type": "ephemeral",
        "text": f"*{label}* → <#{cid}>\nThat channel now gets: {_blurb(name, org)}.",
    }


def _cmd_help() -> dict:
    return {
        "response_type": "ephemeral",
        "text": (
            "*Pulley commands:*\n"
            "• `/pulley open` — list all open PRs\n"
            "• `/pulley me` — list your open PRs\n"
            "• `/pulley team <name>` — list PRs for a team\n"
            "• `/pulley merge [method]` — merge this PR (merge/squash/rebase)\n"
            "• `/pulley settings` — show which channels Pulley posts to\n"
            "• `/pulley settings pr|ci|recap #channel` — change one of them\n"
            "• `/lgtm [comment]` — approve this PR"
        ),
    }
