"""OAuth flows for GitHub and Slack."""

import logging
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import settings
from src.db.queries import (
    get_org_by_github_org_id,
    get_org_by_slack_team,
    link_installation_to_slack_team,
    link_user_slack,
    set_slack_user_token,
    upsert_org_slack,
    upsert_user,
)
from src.services import slack_service

logger = logging.getLogger(__name__)
router = APIRouter()


# ── GitHub OAuth ──────────────────────────────────────────────


@router.get("/github")
async def github_oauth_start():
    params = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": f"{settings.base_url}/auth/github/callback",
            "scope": "read:user,user:email,repo",
        }
    )
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{params}")


@router.get("/github/callback")
async def github_oauth_callback(
    code: str = Query(...),
    state: str = Query(""),
):
    from datetime import UTC, datetime, timedelta

    from src.services.github_service import exchange_code_for_token

    token_data = await exchange_code_for_token(code)
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")
    token_expires_at = (
        datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
    )

    # Linking flow: state=link:<team_id> means the user started from the
    # "Link GitHub organization" button in Slack App Home. Short-circuit the
    # normal user-identity-link path and bind the installation to the workspace.
    if state.startswith("link:"):
        slack_team_id_to_link = state.split(":", 1)[1]
        return await _complete_installation_link(access_token, slack_team_id_to_link)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {access_token}"},
        )
        resp.raise_for_status()
        gh_user = resp.json()

        orgs_resp = await client.get(
            "https://api.github.com/user/orgs",
            headers={"Authorization": f"token {access_token}"},
        )
        orgs_resp.raise_for_status()
        gh_orgs = orgs_resp.json()

    # Parse state — if from Slack App Home, format is "slack_user_id:team_id"
    slack_user_id: str | None = None
    slack_team_id: str | None = None
    if ":" in state:
        parts = state.split(":", 1)
        slack_user_id = parts[0]
        slack_team_id = parts[1]

    # Find the matching org
    org_id: int | None = None
    for gh_org in gh_orgs:
        db_org = await get_org_by_github_org_id(gh_org["id"])
        if db_org:
            org_id = db_org.id
            break

    # If no org matched by GitHub but we have a Slack team, try matching that way
    if not org_id and slack_team_id:
        db_org = await get_org_by_slack_team(slack_team_id)
        if db_org:
            org_id = db_org.id

    if not org_id:
        return HTMLResponse(
            "<h2>Organization not found</h2>"
            "<p>Install the GitHub App and Slack App for your organization first.</p>",
            status_code=400,
        )

    # Create/update the user with GitHub identity
    user = await upsert_user(
        organization_id=org_id,
        github_user_id=gh_user["id"],
        github_username=gh_user["login"],
        github_access_token=access_token,
        github_refresh_token=refresh_token,
        github_token_expires_at=token_expires_at,
        slack_user_id=slack_user_id,
    )

    # If we didn't get Slack ID from state, try email matching
    if not user.slack_user_id:
        email = gh_user.get("email")
        if not email:
            # Fetch emails from GitHub API
            async with httpx.AsyncClient() as client:
                emails_resp = await client.get(
                    "https://api.github.com/user/emails",
                    headers={"Authorization": f"token {access_token}"},
                )
                if emails_resp.status_code == 200:
                    emails = emails_resp.json()
                    primary = next(
                        (e["email"] for e in emails if e.get("primary")),
                        None,
                    )
                    email = primary

        if email:
            slack_match = await slack_service.lookup_user_by_email(email)
            if slack_match:
                await link_user_slack(
                    github_user_id=gh_user["id"],
                    slack_user_id=slack_match["id"],
                )
                logger.info(
                    "Auto-linked %s to Slack user %s via email %s",
                    gh_user["login"],
                    slack_match["id"],
                    email,
                )

    logger.info(
        "GitHub OAuth complete: user=%s slack=%s",
        gh_user["login"],
        slack_user_id or "(email match attempted)",
    )

    return HTMLResponse(
        f"<h2>Connected!</h2>"
        f"<p>GitHub account <strong>{gh_user['login']}</strong> is now linked.</p>"
        f"<p>You can close this window and return to Slack.</p>"
    )


# ── Slack OAuth ──────────────────────────────────────────────


@router.get("/slack")
async def slack_oauth_start():
    params = urlencode(
        {
            "client_id": settings.slack_client_id,
            "scope": "channels:history,channels:manage,channels:read,"
            "chat:write,chat:write.customize,"
            "commands,groups:write,reactions:read,reactions:write,"
            "users:read,users:read.email,bookmarks:write",
            "user_scope": "chat:write,users:read",
            "redirect_uri": f"{settings.base_url}/auth/slack/callback",
        }
    )
    return RedirectResponse(f"https://slack.com/oauth/v2/authorize?{params}")


@router.get("/slack/user")
async def slack_user_oauth_start(slack_user_id: str = Query(...), team_id: str = Query(...)):
    """Per-user Slack OAuth — grants chat:write as the user themselves.

    Lets sync_service post GitHub→Slack messages from the user's identity
    (real avatar, real name, real Slack-message attribution) instead of the
    bot using chat:write.customize to fake it.
    """
    params = urlencode(
        {
            "client_id": settings.slack_client_id,
            "user_scope": "chat:write,users:read",
            "redirect_uri": f"{settings.base_url}/auth/slack/callback",
            "state": f"user:{slack_user_id}:{team_id}",
        }
    )
    return RedirectResponse(f"https://slack.com/oauth/v2/authorize?{params}")


@router.get("/slack/callback")
async def slack_oauth_callback(code: str = Query(...), state: str = Query("")):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
                "redirect_uri": f"{settings.base_url}/auth/slack/callback",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        logger.error("Slack OAuth error: %s", data.get("error"))
        return {"error": data.get("error")}

    # Per-user OAuth path: state=user:<slack_user_id>:<team_id>. Only the user
    # token is meaningful here — the bot was already installed at the workspace.
    if state.startswith("user:"):
        parts = state.split(":")
        if len(parts) >= 2:
            slack_user_id = parts[1]
            user_token = data.get("authed_user", {}).get("access_token")
            if user_token:
                await set_slack_user_token(slack_user_id, user_token)
                logger.info("Stored per-user Slack token for %s", slack_user_id)
                return HTMLResponse(
                    "<h2>Connected!</h2>"
                    "<p>Pulley can now post in Slack as you when GitHub events fire.</p>"
                    "<p>You can close this window and return to Slack.</p>"
                )
        logger.warning("Per-user Slack OAuth missing authed_user.access_token")
        return HTMLResponse(
            "<h2>Connection failed</h2>"
            "<p>Slack didn't return a user token. Try the link in App Home again.</p>",
            status_code=400,
        )

    team = data["team"]
    bot_token = data["access_token"]

    org = await upsert_org_slack(
        slack_team_id=team["id"],
        slack_team_name=team["name"],
        slack_bot_token=bot_token,
    )

    logger.info(
        "Slack OAuth complete: team=%s (%s) org_id=%d",
        team["name"],
        team["id"],
        org.id,
    )

    return HTMLResponse(
        f"<h2>Slack workspace connected!</h2>"
        f"<p>Team <strong>{team['name']}</strong> is now linked to Pulley.</p>"
        f"<p>You can close this window.</p>"
    )


# ── Installation linking ─────────────────────────────────────
# Returns installations of OUR GitHub App that the authenticated user can
# actually see (scoped by their org memberships on GitHub's side — no
# cross-tenant leakage), then binds exactly one to the initiating Slack team.


async def _complete_installation_link(user_access_token: str, slack_team_id: str) -> HTMLResponse:
    app_id = settings.github_app_id
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user/installations",
            headers={"Authorization": f"token {user_access_token}"},
            params={"app_id": app_id},
        )
        resp.raise_for_status()
        body = resp.json()

    # API returns all installations visible to the user regardless of app_id;
    # filter client-side as a defense-in-depth check.
    installations = [
        inst for inst in body.get("installations", []) if str(inst.get("app_id")) == str(app_id)
    ]

    if not installations:
        return HTMLResponse(
            "<h2>No Pulley installations found</h2>"
            "<p>You don't have access to any GitHub organizations that have "
            "Pulley installed. Ask an org owner to install the app, then try again.</p>",
            status_code=404,
        )

    if len(installations) > 1:
        # Don't auto-pick — let the user pick explicitly. (MVP: just show names
        # and instruct them to re-link from the correct org's admin account.)
        names = ", ".join(inst["account"]["login"] for inst in installations)
        return HTMLResponse(
            "<h2>Multiple installations visible</h2>"
            f"<p>You can see Pulley installed on: {names}. "
            "Linking is currently limited to a single visible installation per request.</p>",
            status_code=409,
        )

    inst = installations[0]
    account = inst["account"]
    org = await link_installation_to_slack_team(
        installation_id=inst["id"],
        github_org_id=account["id"],
        github_org_login=account["login"],
        slack_team_id=slack_team_id,
    )
    logger.info(
        "Linked installation %d (%s) to Slack team %s → org db_id=%d",
        inst["id"],
        account["login"],
        slack_team_id,
        org.id,
    )
    return HTMLResponse(
        f"<h2>Linked!</h2>"
        f"<p>GitHub organization <strong>{account['login']}</strong> is now connected "
        f"to this Slack workspace.</p>"
        f"<p>You can close this window and return to Slack.</p>"
    )
