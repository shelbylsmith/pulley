"""GitHub App installation lifecycle — creates/updates/deletes Organization records."""

import logging

from src.db.queries import get_org_by_installation, upsert_organization

logger = logging.getLogger(__name__)


async def handle_installation(payload: dict, action: str | None) -> None:
    installation = payload["installation"]
    installation_id = installation["id"]
    account = installation["account"]

    if action == "created":
        org = await upsert_organization(
            github_org_id=account["id"],
            github_org_login=account["login"],
            github_installation_id=installation_id,
        )
        logger.info(
            "GitHub App installed: org=%s installation=%d db_id=%d",
            account["login"],
            installation_id,
            org.id,
        )

    elif action == "deleted":
        org = await get_org_by_installation(installation_id)
        if org:
            logger.info(
                "GitHub App uninstalled: org=%s installation=%d",
                org.github_org_login,
                installation_id,
            )
            # Keep the org record for history — Slack side may still be active

    elif action == "suspend":
        logger.info("GitHub App suspended for installation=%d", installation_id)

    elif action == "unsuspend":
        org = await upsert_organization(
            github_org_id=account["id"],
            github_org_login=account["login"],
            github_installation_id=installation_id,
        )
        logger.info("GitHub App unsuspended for installation=%d", installation_id)

    elif action == "new_permissions_accepted":
        logger.info(
            "New permissions accepted for installation=%d",
            installation_id,
        )
