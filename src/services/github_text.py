"""Rendering GitHub-authored prose for Slack."""

from src.db.queries import get_slack_id_map_for_github_usernames
from src.utils.markdown import gfm_to_slack, github_mention_logins


async def github_body_to_slack(body: str) -> str:
    """Render a GitHub comment or issue body as Slack mrkdwn, turning @mentions
    of linked users into Slack pings. Unlinked logins stay literal.
    """
    logins = github_mention_logins(body)
    mentions = await get_slack_id_map_for_github_usernames(logins) if logins else {}
    return gfm_to_slack(body, mentions)
