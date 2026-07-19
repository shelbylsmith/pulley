"""Slack App Home — onboarding UI and org reconciliation."""

import logging
from urllib.parse import urlencode

from src.config import settings
from src.db.queries import get_org_by_slack_team, get_user_by_slack_id, upsert_org_slack
from src.services import slack_service

logger = logging.getLogger(__name__)


async def handle_app_home_opened(
    slack_user_id: str,
    team_id: str,
    bot_token: str,
) -> None:
    """Called when a user opens the Pulley App Home tab in Slack."""
    # Reconcile: ensure the org exists even if we missed the OAuth callback
    org = await get_org_by_slack_team(team_id)
    if not org:
        org = await upsert_org_slack(
            slack_team_id=team_id,
            slack_team_name="",
            slack_bot_token=bot_token,
        )
        logger.info("Reconciled missing org for team %s (db_id=%d)", team_id, org.id)

    # Check if user has linked their GitHub account
    user = await get_user_by_slack_id(slack_user_id)

    blocks = _build_home(user, slack_user_id, team_id, org)
    await slack_service.publish_home(slack_user_id, blocks, token=bot_token)


def _build_home(user, slack_user_id: str, team_id: str, org) -> list[dict]:
    """Single App Home view. Each section shows state + a button to act on it."""
    connect_url = _github_connect_url(slack_user_id, team_id)
    link_org_url = _github_link_url(team_id)
    slack_user_url = _slack_user_oauth_url(slack_user_id, team_id)

    # Personal GitHub link
    if user and user.github_username:
        personal_text = (
            "*Your GitHub account*\n"
            f"Connected as *{user.github_username}*. "
            "Click *Reconnect* if you need to refresh scopes or switch accounts."
        )
        personal_button_text = "Reconnect"
        personal_button_style = None
    else:
        personal_text = (
            "*Your GitHub account*\n"
            "Link your personal GitHub account so Pulley can invite you to PR "
            "channels, post your Slack comments as you on GitHub, and let you "
            "merge / approve PRs from Slack."
        )
        personal_button_text = "Connect GitHub"
        personal_button_style = "primary"

    personal_button: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": personal_button_text},
        "url": connect_url,
    }
    if personal_button_style:
        personal_button["style"] = personal_button_style

    # Per-user Slack OAuth (lets Pulley post on Slack as the user themselves
    # for GitHub→Slack syncs, instead of the bot impersonating their name+icon).
    if user and user.slack_user_token:
        slack_user_text = (
            "*Post as you on Slack*\n"
            "Granted. Pulley will post GitHub events as you natively in Slack."
        )
        slack_user_button_text = "Reauthorize"
    else:
        slack_user_text = (
            "*Post as you on Slack*\n"
            "Without this, Pulley posts GitHub events using your name and avatar "
            "as a custom bot identity. Grant Slack permission to post natively as you."
        )
        slack_user_button_text = "Authorize"

    # Workspace ↔ GitHub org link
    if org.github_installation_id and org.github_org_login:
        org_text = (
            f"*Workspace ↔ GitHub organization*\n"
            f"Linked to `{org.github_org_login}`. Click *Relink* only to change "
            "which GitHub org this workspace is bound to."
        )
        org_button_text = "Relink"
    else:
        org_text = (
            "*Workspace ↔ GitHub organization*\n"
            "One-time admin setup. Binds this Slack workspace to a GitHub org "
            "where the Pulley App is installed. Until done, PR webhooks won't "
            "open channels here."
        )
        org_button_text = "Link organization"

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Pulley"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Pulley creates a Slack channel for every pull request, "
                    "syncs comments bidirectionally, and keeps your team "
                    "on top of code reviews."
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": personal_text},
            "accessory": personal_button,
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": slack_user_text},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": slack_user_button_text},
                "url": slack_user_url,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": org_text},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": org_button_text},
                "url": link_org_url,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Commands:*\n"
                    "• `/pulley open` — list all open PRs\n"
                    "• `/pulley me` — list your open PRs\n"
                    "• `/pulley team <name>` — list PRs for a team\n"
                    "• `/pulley merge [method]` — merge this PR\n"
                    "• `/pulley settings` — configure channels\n"
                    "• `/lgtm [comment]` — approve this PR"
                ),
            },
        },
    ]


def _github_connect_url(slack_user_id: str, team_id: str) -> str:
    """Build GitHub OAuth URL with Slack identity in state param."""
    state = f"{slack_user_id}:{team_id}"
    params = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": f"{settings.base_url}/auth/github/callback",
            "scope": "read:user,user:email,repo",
            "state": state,
        }
    )
    return f"https://github.com/login/oauth/authorize?{params}"


def _github_link_url(team_id: str) -> str:
    """Build GitHub OAuth URL for binding a GitHub installation to this Slack team."""
    params = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": f"{settings.base_url}/auth/github/callback",
            "scope": "read:user",
            "state": f"link:{team_id}",
        }
    )
    return f"https://github.com/login/oauth/authorize?{params}"


def _slack_user_oauth_url(slack_user_id: str, team_id: str) -> str:
    """Per-user Slack OAuth URL — grants chat:write user-scope so the app can
    post messages as the user themselves (not bot+customize impersonation).
    """
    return f"{settings.base_url}/auth/slack/user?slack_user_id={slack_user_id}&team_id={team_id}"
