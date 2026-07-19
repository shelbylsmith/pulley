"""GitHub App API client — handles JWT auth, installation tokens, and REST calls."""

import time
from dataclasses import dataclass

import httpx
import jwt

from src.config import settings

GITHUB_API = "https://api.github.com"


@dataclass
class InstallationToken:
    token: str
    expires_at: float


_token_cache: dict[int, InstallationToken] = {}


def _make_jwt() -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": settings.github_app_id,
    }
    return jwt.encode(payload, settings.github_private_key_pem, algorithm="RS256")


async def _get_installation_token(installation_id: int) -> str:
    cached = _token_cache.get(installation_id)
    if cached and cached.expires_at > time.time() + 60:
        return cached.token

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {_make_jwt()}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache[installation_id] = InstallationToken(
        token=data["token"],
        expires_at=time.time() + 3500,
    )
    return data["token"]


async def github_request(
    method: str,
    path: str,
    installation_id: int,
    *,
    json: dict | None = None,
) -> httpx.Response:
    token = await _get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method,
            f"{GITHUB_API}{path}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            json=json,
        )
    return resp


async def get_user_email(installation_id: int, username: str) -> str | None:
    """Fetch a GitHub user's public email via the API."""
    resp = await github_request("GET", f"/users/{username}", installation_id)
    if resp.status_code == 200:
        return resp.json().get("email")
    return None


async def get_pull_request(installation_id: int, repo: str, pr_number: int) -> dict:
    resp = await github_request("GET", f"/repos/{repo}/pulls/{pr_number}", installation_id)
    resp.raise_for_status()
    return resp.json()


async def get_commit(installation_id: int, repo: str, sha: str) -> dict:
    resp = await github_request("GET", f"/repos/{repo}/commits/{sha}", installation_id)
    resp.raise_for_status()
    return resp.json()


async def get_pull_request_reviews(installation_id: int, repo: str, pr_number: int) -> list[dict]:
    resp = await github_request("GET", f"/repos/{repo}/pulls/{pr_number}/reviews", installation_id)
    resp.raise_for_status()
    return resp.json()


async def create_issue_comment(
    installation_id: int, repo: str, issue_number: int, body: str
) -> dict:
    resp = await github_request(
        "POST",
        f"/repos/{repo}/issues/{issue_number}/comments",
        installation_id,
        json={"body": body},
    )
    resp.raise_for_status()
    return resp.json()


async def create_issue_comment_as_user(
    user_access_token: str, repo: str, issue_number: int, body: str
) -> dict:
    """Post a comment attributed to the user themselves. Requires a user OAuth
    token with `repo` (or `public_repo` for public-only) scope.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{repo}/issues/{issue_number}/comments",
            headers={
                "Authorization": f"token {user_access_token}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": body},
        )
    resp.raise_for_status()
    return resp.json()


async def reply_to_review_comment_as_user(
    user_access_token: str, repo: str, pr_number: int, comment_id: int, body: str
) -> dict:
    """Reply in an existing PR review thread (not a plain issue comment)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
            headers={
                "Authorization": f"token {user_access_token}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": body},
        )
    resp.raise_for_status()
    return resp.json()


async def _user_request(
    method: str,
    path: str,
    user_access_token: str,
    *,
    json: dict | None = None,
) -> httpx.Response:
    """Make a GitHub REST call attributed to the user via their OAuth token."""
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method,
            f"{GITHUB_API}{path}",
            headers={
                "Authorization": f"token {user_access_token}",
                "Accept": "application/vnd.github+json",
            },
            json=json,
        )
    return resp


async def update_issue_comment(installation_id: int, repo: str, comment_id: int, body: str) -> dict:
    resp = await github_request(
        "PATCH",
        f"/repos/{repo}/issues/comments/{comment_id}",
        installation_id,
        json={"body": body},
    )
    resp.raise_for_status()
    return resp.json()


async def update_issue_comment_as_user(
    user_access_token: str, repo: str, comment_id: int, body: str
) -> dict:
    resp = await _user_request(
        "PATCH",
        f"/repos/{repo}/issues/comments/{comment_id}",
        user_access_token,
        json={"body": body},
    )
    resp.raise_for_status()
    return resp.json()


async def delete_issue_comment(installation_id: int, repo: str, comment_id: int) -> None:
    resp = await github_request(
        "DELETE", f"/repos/{repo}/issues/comments/{comment_id}", installation_id
    )
    resp.raise_for_status()


async def delete_issue_comment_as_user(user_access_token: str, repo: str, comment_id: int) -> None:
    resp = await _user_request(
        "DELETE", f"/repos/{repo}/issues/comments/{comment_id}", user_access_token
    )
    resp.raise_for_status()


async def update_review_comment(
    installation_id: int, repo: str, comment_id: int, body: str
) -> dict:
    resp = await github_request(
        "PATCH",
        f"/repos/{repo}/pulls/comments/{comment_id}",
        installation_id,
        json={"body": body},
    )
    resp.raise_for_status()
    return resp.json()


async def update_review_comment_as_user(
    user_access_token: str, repo: str, comment_id: int, body: str
) -> dict:
    resp = await _user_request(
        "PATCH",
        f"/repos/{repo}/pulls/comments/{comment_id}",
        user_access_token,
        json={"body": body},
    )
    resp.raise_for_status()
    return resp.json()


async def delete_review_comment(installation_id: int, repo: str, comment_id: int) -> None:
    resp = await github_request(
        "DELETE", f"/repos/{repo}/pulls/comments/{comment_id}", installation_id
    )
    resp.raise_for_status()


async def delete_review_comment_as_user(user_access_token: str, repo: str, comment_id: int) -> None:
    resp = await _user_request(
        "DELETE", f"/repos/{repo}/pulls/comments/{comment_id}", user_access_token
    )
    resp.raise_for_status()


async def create_review_as_user(
    user_access_token: str, repo: str, pr_number: int, event: str, body: str = ""
) -> dict:
    """Submit a PR review attributed to the user. Required for APPROVE since
    the bot's approval doesn't count toward branch-protection requirements.
    """
    payload: dict = {"event": event}
    if body:
        payload["body"] = body
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews",
            headers={
                "Authorization": f"token {user_access_token}",
                "Accept": "application/vnd.github+json",
            },
            json=payload,
        )
    resp.raise_for_status()
    return resp.json()


async def merge_pull_request(
    installation_id: int, repo: str, pr_number: int, merge_method: str = "merge"
) -> dict:
    resp = await github_request(
        "PUT",
        f"/repos/{repo}/pulls/{pr_number}/merge",
        installation_id,
        json={"merge_method": merge_method},
    )
    resp.raise_for_status()
    return resp.json()


async def get_check_runs_for_ref(installation_id: int, repo: str, ref: str) -> list[dict]:
    """List all check-runs for a commit ref. Used to compute aggregate CI state for a PR."""
    path = f"/repos/{repo}/commits/{ref}/check-runs?per_page=100"
    resp = await github_request("GET", path, installation_id)
    resp.raise_for_status()
    return resp.json().get("check_runs", [])


async def get_check_runs_for_suite(
    installation_id: int, repo: str, check_suite_id: int
) -> list[dict]:
    """List check-runs belonging to a specific check_suite."""
    path = f"/repos/{repo}/check-suites/{check_suite_id}/check-runs?per_page=100"
    resp = await github_request("GET", path, installation_id)
    resp.raise_for_status()
    return resp.json().get("check_runs", [])


async def get_workflow_run(installation_id: int, repo: str, run_id: int) -> dict:
    """Fetch a GitHub Actions workflow run by id (for its name, etc.)."""
    resp = await github_request("GET", f"/repos/{repo}/actions/runs/{run_id}", installation_id)
    resp.raise_for_status()
    return resp.json()


async def get_open_pulls(installation_id: int, repo: str) -> list[dict]:
    path = f"/repos/{repo}/pulls?state=open&per_page=100"
    resp = await github_request("GET", path, installation_id)
    resp.raise_for_status()
    return resp.json()


async def get_team_members(installation_id: int, org: str, team_slug: str) -> list[dict]:
    path = f"/orgs/{org}/teams/{team_slug}/members?per_page=100"
    resp = await github_request("GET", path, installation_id)
    resp.raise_for_status()
    return resp.json()


async def list_installation_repos(installation_id: int) -> list[dict]:
    resp = await github_request("GET", "/installation/repositories?per_page=100", installation_id)
    resp.raise_for_status()
    return resp.json()["repositories"]


async def exchange_code_for_token(code: str) -> dict:
    """Exchange OAuth code for a user access token (+ refresh_token if expiry enabled)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_user_token(refresh_token: str) -> dict:
    """Use a refresh token to get a new access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_valid_user_token(user) -> str | None:
    """Return a valid access token for a user, refreshing if expired."""
    from datetime import UTC, datetime, timedelta

    from src.db.queries import update_user_tokens

    if not user.github_access_token:
        return None

    # If no expiry tracked, assume valid (pre-expiry-feature tokens)
    if not user.github_token_expires_at:
        return user.github_access_token

    # Refresh if expiring within 5 minutes
    if user.github_token_expires_at <= datetime.now(UTC) + timedelta(minutes=5):
        if not user.github_refresh_token:
            return None

        data = await refresh_user_token(user.github_refresh_token)
        expires_in = data.get("expires_in", 28800)
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        await update_user_tokens(
            github_user_id=user.github_user_id,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", user.github_refresh_token),
            expires_at=expires_at,
        )
        return data["access_token"]

    return user.github_access_token
