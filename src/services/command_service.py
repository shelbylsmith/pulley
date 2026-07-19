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


async def _cmd_settings(arg: str, team_id: str, channel_id: str) -> dict:
    from src.db.queries import get_org_by_slack_team, update_org_settings

    org = await get_org_by_slack_team(team_id)
    if not org:
        return {"response_type": "ephemeral", "text": "Organization not connected."}

    parts = arg.split(maxsplit=1)
    setting = parts[0].lower() if parts else ""
    value = parts[1].strip() if len(parts) > 1 else ""

    # Strip Slack's channel formatting: <#C12345|channel-name> → C12345.
    # If the user just typed `#foo` (no channel link), strip the leading `#`
    # so we don't end up rendering `<##foo>` in the settings display.
    def _parse_channel_id(raw: str) -> str:
        if raw.startswith("<#") and "|" in raw:
            return raw[2 : raw.index("|")]
        if raw.startswith("<#") and raw.endswith(">"):
            return raw[2:-1]
        return raw.lstrip("#")

    if setting == "recap":
        if not value:
            if org.recap_channel_id:
                return {
                    "response_type": "ephemeral",
                    "text": f"Recap channel: <#{org.recap_channel_id}>",
                }
            return {"response_type": "ephemeral", "text": "No recap channel configured."}

        cid = _parse_channel_id(value)
        await update_org_settings(org.id, recap_channel_id=cid)
        return {
            "response_type": "ephemeral",
            "text": f"Recap channel set to <#{cid}>",
        }

    elif setting == "ci":
        if not value:
            if org.ci_channel_id:
                return {
                    "response_type": "ephemeral",
                    "text": f"CI channel: <#{org.ci_channel_id}>",
                }
            return {
                "response_type": "ephemeral",
                "text": "No CI channel configured.",
            }

        cid = _parse_channel_id(value)
        await update_org_settings(org.id, ci_channel_id=cid)
        return {
            "response_type": "ephemeral",
            "text": f"CI channel set to <#{cid}>",
        }

    elif setting == "pr":
        if not value:
            if org.pr_channel_id:
                return {
                    "response_type": "ephemeral",
                    "text": f"PR digest channel: <#{org.pr_channel_id}>",
                }
            return {
                "response_type": "ephemeral",
                "text": "No PR digest channel configured.",
            }

        cid = _parse_channel_id(value)
        await update_org_settings(org.id, pr_channel_id=cid)
        return {
            "response_type": "ephemeral",
            "text": f"PR digest channel set to <#{cid}>",
        }

    else:
        recap = f"<#{org.recap_channel_id}>" if org.recap_channel_id else "_not set_"
        ci = f"<#{org.ci_channel_id}>" if org.ci_channel_id else "_not set_"
        pr_ch = f"<#{org.pr_channel_id}>" if org.pr_channel_id else "_not set_"

        if org.github_installation_id and org.github_org_login:
            gh_status = f"✅ Linked to *{org.github_org_login}*"
        else:
            gh_status = (
                "⚠️ Not linked — open the Pulley App Home and click "
                "*Link organization* to connect a GitHub org"
            )

        lines = [
            "*Pulley settings:*",
            f"  GitHub: {gh_status}",
            f"  Recap channel: {recap}",
            f"  CI channel: {ci}",
            f"  PR digest channel: {pr_ch}",
            "",
            "*Usage:*",
            "  `/pulley settings recap #channel`",
            "  `/pulley settings ci #channel`",
            "  `/pulley settings pr #channel`",
        ]
        return {"response_type": "ephemeral", "text": "\n".join(lines)}


def _cmd_help() -> dict:
    return {
        "response_type": "ephemeral",
        "text": (
            "*Pulley commands:*\n"
            "• `/pulley open` — list all open PRs\n"
            "• `/pulley me` — list your open PRs\n"
            "• `/pulley team <name>` — list PRs for a team\n"
            "• `/pulley merge [method]` — merge this PR (merge/squash/rebase)\n"
            "• `/pulley settings` — configure channels\n"
            "• `/lgtm [comment]` — approve this PR"
        ),
    }
