"""Database query functions used across services."""

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.database import async_session
from src.models.message_mapping import MessageMapping
from src.models.organization import Organization
from src.models.pull_request import PullRequest
from src.models.scheduler_run import SchedulerRun
from src.models.slack_channel import SlackChannel
from src.models.slack_event_claim import SlackEventClaim
from src.models.thread_mapping import ThreadMapping
from src.models.user import User

# ── Context manager ───────────────────────────────────────


def _session() -> AsyncSession:
    return async_session()


# ── Organization queries ──────────────────────────────────


async def get_org_by_slack_team(team_id: str) -> Organization | None:
    async with _session() as s:
        result = await s.execute(select(Organization).where(Organization.slack_team_id == team_id))
        return result.scalar_one_or_none()


async def get_org_by_github_org_id(github_org_id: int) -> Organization | None:
    async with _session() as s:
        result = await s.execute(
            select(Organization).where(Organization.github_org_id == github_org_id)
        )
        return result.scalar_one_or_none()


async def get_org_by_installation(installation_id: int) -> Organization | None:
    async with _session() as s:
        result = await s.execute(
            select(Organization).where(Organization.github_installation_id == installation_id)
        )
        return result.scalar_one_or_none()


async def get_org(org_id: int) -> Organization | None:
    async with _session() as s:
        return await s.get(Organization, org_id)


async def get_all_orgs_with_recap() -> list[Organization]:
    async with _session() as s:
        result = await s.execute(
            select(Organization).where(Organization.recap_channel_id.isnot(None))
        )
        return list(result.scalars().all())


async def get_all_orgs() -> list[Organization]:
    async with _session() as s:
        result = await s.execute(select(Organization))
        return list(result.scalars().all())


async def update_org_settings(org_id: int, **fields: object) -> None:
    async with _session() as s:
        org = await s.get(Organization, org_id)
        if org:
            for k, v in fields.items():
                setattr(org, k, v)
            await s.commit()


# ── User queries ──────────────────────────────────────────


async def get_user_by_slack_id(slack_user_id: str) -> User | None:
    async with _session() as s:
        result = await s.execute(select(User).where(User.slack_user_id == slack_user_id))
        return result.scalar_one_or_none()


async def get_slack_ids_for_github_usernames(
    usernames: list[str],
) -> list[str]:
    """Return Slack user IDs for the given GitHub usernames (skips unknowns)."""
    if not usernames:
        return []
    async with _session() as s:
        result = await s.execute(
            select(User.slack_user_id)
            .where(User.github_username.in_(usernames))
            .where(User.slack_user_id.isnot(None))
        )
        return list(result.scalars().all())


async def get_slack_id_map_for_github_usernames(
    usernames: set[str],
) -> dict[str, str]:
    """Map each given GitHub username (lowercased) to its Slack user ID, omitting
    users with no Slack link. Case-insensitive — GitHub logins are.
    """
    if not usernames:
        return {}
    lowered = {u.lower() for u in usernames}
    async with _session() as s:
        result = await s.execute(
            select(User.github_username, User.slack_user_id)
            .where(func.lower(User.github_username).in_(lowered))
            .where(User.slack_user_id.isnot(None))
        )
        return {login.lower(): slack_id for login, slack_id in result.all()}


async def get_github_username_map_for_slack_ids(
    slack_ids: set[str],
) -> dict[str, str]:
    """Map each given Slack user ID to its GitHub username, omitting users with
    no GitHub link.
    """
    if not slack_ids:
        return {}
    async with _session() as s:
        result = await s.execute(
            select(User.slack_user_id, User.github_username).where(
                User.slack_user_id.in_(slack_ids)
            )
        )
        return {slack_id: username for slack_id, username in result.all()}


async def get_user_with_timeslots(
    slack_user_id: str,
) -> User | None:
    async with _session() as s:
        result = await s.execute(
            select(User)
            .where(User.slack_user_id == slack_user_id)
            .options(selectinload(User.review_time_slots))
        )
        return result.scalar_one_or_none()


# ── PullRequest queries ──────────────────────────────────


async def get_pr_by_repo_and_number(repo_full_name: str, pr_number: int) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest)
            .where(PullRequest.repo_full_name == repo_full_name)
            .where(PullRequest.github_pr_number == pr_number)
        )
        return result.scalar_one_or_none()


async def get_pr_by_github_id(github_pr_id: int) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest).where(PullRequest.github_pr_id == github_pr_id)
        )
        return result.scalar_one_or_none()


async def get_pr_by_channel(slack_channel_id: str) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest).where(PullRequest.slack_channel_id == slack_channel_id)
        )
        return result.scalar_one_or_none()


async def get_open_prs_for_org(slack_team_id: str) -> list[PullRequest]:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest)
            .join(Organization)
            .where(Organization.slack_team_id == slack_team_id)
            .where(PullRequest.state == "open")
            .order_by(PullRequest.created_at.desc())
        )
        return list(result.scalars().all())


async def get_open_prs_for_author(github_user_id: int) -> list[PullRequest]:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest)
            .where(PullRequest.author_github_id == github_user_id)
            .where(PullRequest.state == "open")
            .order_by(PullRequest.created_at.desc())
        )
        return list(result.scalars().all())


# ── Mutations ─────────────────────────────────────────────


async def upsert_organization(
    *,
    github_org_id: int,
    github_org_login: str,
    github_installation_id: int,
    slack_team_id: str | None = None,
    slack_team_name: str | None = None,
    slack_bot_token: str | None = None,
) -> Organization:
    async with _session() as s:
        result = await s.execute(
            select(Organization).where(Organization.github_org_id == github_org_id)
        )
        org = result.scalar_one_or_none()
        if org:
            org.github_org_login = github_org_login
            org.github_installation_id = github_installation_id
            if slack_team_id is not None:
                org.slack_team_id = slack_team_id
            if slack_team_name is not None:
                org.slack_team_name = slack_team_name
            if slack_bot_token is not None:
                org.slack_bot_token = slack_bot_token
        else:
            org = Organization(
                github_org_id=github_org_id,
                github_org_login=github_org_login,
                github_installation_id=github_installation_id,
                slack_team_id=slack_team_id,
                slack_team_name=slack_team_name,
                slack_bot_token=slack_bot_token,
            )
            s.add(org)
        await s.commit()
        await s.refresh(org)
        return org


async def upsert_org_slack(
    *,
    slack_team_id: str,
    slack_team_name: str,
    slack_bot_token: str,
) -> Organization:
    """Update an existing org's Slack details, or create a placeholder if none exists yet."""
    async with _session() as s:
        result = await s.execute(
            select(Organization).where(Organization.slack_team_id == slack_team_id)
        )
        org = result.scalar_one_or_none()
        if org:
            org.slack_team_name = slack_team_name
            org.slack_bot_token = slack_bot_token
        else:
            org = Organization(
                github_org_id=0,
                github_org_login="",
                github_installation_id=0,
                slack_team_id=slack_team_id,
                slack_team_name=slack_team_name,
                slack_bot_token=slack_bot_token,
            )
            s.add(org)
        await s.commit()
        await s.refresh(org)
        return org


async def link_installation_to_slack_team(
    *,
    installation_id: int,
    github_org_id: int,
    github_org_login: str,
    slack_team_id: str,
) -> Organization:
    """Explicit merge: bind a GitHub installation to a Slack workspace.

    Called from the OAuth-based linking flow after authority on both sides has
    been proven (the same user just authed to GitHub and owns the Slack session
    that initiated the flow). Handles the three possible starting states:

      1. Slack row exists (slack-only, github_*=0): fill in GitHub fields.
      2. GitHub row exists (github-only, slack_*=NULL) from the webhook backfill:
         copy its PR/user FKs onto the Slack row (or the other way), then delete
         the orphan so we end with one row.
      3. Both exist separately (the stuck state): merge FKs → keep the GitHub row,
         move Slack fields into it, delete the Slack-only row.

    Returns the surviving org row.
    """
    async with _session() as s:
        slack_row = (
            await s.execute(select(Organization).where(Organization.slack_team_id == slack_team_id))
        ).scalar_one_or_none()
        gh_row = (
            await s.execute(
                select(Organization).where(Organization.github_installation_id == installation_id)
            )
        ).scalar_one_or_none()

        if slack_row and gh_row and slack_row.id != gh_row.id:
            # Both orphans present — migrate FKs from slack_row → gh_row, then delete slack_row.
            from sqlalchemy import update

            await s.execute(
                update(User)
                .where(User.organization_id == slack_row.id)
                .values(organization_id=gh_row.id)
            )
            await s.execute(
                update(PullRequest)
                .where(PullRequest.organization_id == slack_row.id)
                .values(organization_id=gh_row.id)
            )
            gh_row.slack_team_id = slack_row.slack_team_id
            gh_row.slack_team_name = slack_row.slack_team_name
            gh_row.slack_bot_token = slack_row.slack_bot_token
            await s.delete(slack_row)
            survivor = gh_row
        elif gh_row:
            # GitHub-only: caller (Slack OAuth already happened somewhere) fills in slack fields
            survivor = gh_row
        elif slack_row:
            slack_row.github_org_id = github_org_id
            slack_row.github_org_login = github_org_login
            slack_row.github_installation_id = installation_id
            survivor = slack_row
        else:
            survivor = Organization(
                github_org_id=github_org_id,
                github_org_login=github_org_login,
                github_installation_id=installation_id,
                slack_team_id=slack_team_id,
            )
            s.add(survivor)

        await s.commit()
        await s.refresh(survivor)
        return survivor


async def upsert_user(
    *,
    organization_id: int,
    github_user_id: int,
    github_username: str,
    github_access_token: str | None = None,
    github_refresh_token: str | None = None,
    github_token_expires_at: datetime | None = None,
    slack_user_id: str | None = None,
    slack_user_token: str | None = None,
) -> User:
    async with _session() as s:
        result = await s.execute(select(User).where(User.github_user_id == github_user_id))
        user = result.scalar_one_or_none()
        if user:
            user.github_username = github_username
            if github_access_token is not None:
                user.github_access_token = github_access_token
            if github_refresh_token is not None:
                user.github_refresh_token = github_refresh_token
            if github_token_expires_at is not None:
                user.github_token_expires_at = github_token_expires_at
            if slack_user_id is not None:
                user.slack_user_id = slack_user_id
            if slack_user_token is not None:
                user.slack_user_token = slack_user_token
            user.is_onboarded = True
        else:
            user = User(
                organization_id=organization_id,
                github_user_id=github_user_id,
                github_username=github_username,
                github_access_token=github_access_token,
                github_refresh_token=github_refresh_token,
                github_token_expires_at=github_token_expires_at,
                slack_user_id=slack_user_id,
                slack_user_token=slack_user_token,
                is_onboarded=True,
            )
            s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def update_user_tokens(
    *,
    github_user_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
) -> None:
    async with _session() as s:
        result = await s.execute(select(User).where(User.github_user_id == github_user_id))
        user = result.scalar_one_or_none()
        if user:
            user.github_access_token = access_token
            user.github_refresh_token = refresh_token
            user.github_token_expires_at = expires_at
            await s.commit()


async def link_user_slack(
    *,
    github_user_id: int,
    slack_user_id: str,
    slack_user_token: str | None = None,
) -> User | None:
    async with _session() as s:
        result = await s.execute(select(User).where(User.github_user_id == github_user_id))
        user = result.scalar_one_or_none()
        if not user:
            return None
        user.slack_user_id = slack_user_id
        user.slack_user_token = slack_user_token
        user.is_onboarded = True
        await s.commit()
        await s.refresh(user)
        return user


async def create_pr_and_channel(
    *,
    organization_id: int,
    github_pr_id: int,
    github_pr_number: int,
    repo_full_name: str,
    title: str,
    is_draft: bool,
    head_branch: str,
    base_branch: str,
    html_url: str,
    author_github_id: int,
    author_github_username: str,
    slack_channel_id: str,
    slack_channel_name: str,
    title_bookmark_id: str | None = None,
) -> PullRequest:
    async with _session() as s:
        pr = PullRequest(
            organization_id=organization_id,
            github_pr_id=github_pr_id,
            github_pr_number=github_pr_number,
            repo_full_name=repo_full_name,
            title=title,
            is_draft=is_draft,
            head_branch=head_branch,
            base_branch=base_branch,
            html_url=html_url,
            author_github_id=author_github_id,
            author_github_username=author_github_username,
            slack_channel_id=slack_channel_id,
            title_bookmark_id=title_bookmark_id,
        )
        s.add(pr)
        await s.flush()

        channel = SlackChannel(
            pull_request_id=pr.id,
            slack_channel_id=slack_channel_id,
            slack_channel_name=slack_channel_name,
        )
        s.add(channel)
        await s.commit()
        await s.refresh(pr)
        return pr


async def set_pr_digest_ts(pr_id: int, ts: str) -> None:
    async with _session() as s:
        pr = await s.get(PullRequest, pr_id)
        if pr:
            pr.pr_digest_ts = ts
            await s.commit()


async def set_pr_ci_bookmark_id(pr_id: int, bookmark_id: str) -> None:
    async with _session() as s:
        pr = await s.get(PullRequest, pr_id)
        if pr:
            pr.ci_bookmark_id = bookmark_id
            await s.commit()


async def transition_pr_last_ci_state(pr_id: int, state: str) -> bool:
    """Atomically set last_ci_state if it differs. Returns True if the row
    changed — caller uses this to decide whether to post a recap. Prevents
    duplicate posts when check_suite and workflow_run events race.
    """
    async with _session() as s:
        result = await s.execute(
            update(PullRequest)
            .where(PullRequest.id == pr_id)
            .where((PullRequest.last_ci_state.is_(None)) | (PullRequest.last_ci_state != state))
            .values(last_ci_state=state)
        )
        await s.commit()
        return result.rowcount > 0


async def set_pr_last_review_state(github_pr_id: int, state: str | None) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest).where(PullRequest.github_pr_id == github_pr_id)
        )
        pr = result.scalar_one_or_none()
        if pr:
            pr.last_review_state = state
            await s.commit()
            await s.refresh(pr)
        return pr


async def set_slack_user_token(slack_user_id: str, slack_user_token: str) -> User | None:
    """Persist a per-user Slack OAuth token (user_scope=chat:write,users:read).

    Used by the per-user Slack OAuth flow so that GitHub→Slack sync messages
    can be posted by the actual Slack user instead of the bot impersonating
    via chat:write.customize.
    """
    async with _session() as s:
        result = await s.execute(select(User).where(User.slack_user_id == slack_user_id))
        user = result.scalar_one_or_none()
        if user:
            user.slack_user_token = slack_user_token
            await s.commit()
            await s.refresh(user)
        return user


async def get_user_by_github_username(github_username: str) -> User | None:
    async with _session() as s:
        result = await s.execute(select(User).where(User.github_username == github_username))
        return result.scalar_one_or_none()


async def set_pr_reviewers(github_pr_id: int, usernames: list[str]) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest).where(PullRequest.github_pr_id == github_pr_id)
        )
        pr = result.scalar_one_or_none()
        if pr:
            pr.reviewers = ",".join(usernames) if usernames else None
            await s.commit()
            await s.refresh(pr)
        return pr


async def set_pr_title(github_pr_id: int, title: str) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest).where(PullRequest.github_pr_id == github_pr_id)
        )
        pr = result.scalar_one_or_none()
        if pr:
            pr.title = title
            await s.commit()
            await s.refresh(pr)
        return pr


async def set_pr_base_branch(github_pr_id: int, base_branch: str) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest).where(PullRequest.github_pr_id == github_pr_id)
        )
        pr = result.scalar_one_or_none()
        if pr:
            pr.base_branch = base_branch
            await s.commit()
            await s.refresh(pr)
        return pr


async def set_pr_draft_state(github_pr_id: int, is_draft: bool) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest).where(PullRequest.github_pr_id == github_pr_id)
        )
        pr = result.scalar_one_or_none()
        if pr:
            pr.is_draft = is_draft
            await s.commit()
            await s.refresh(pr)
        return pr


async def close_pr(github_pr_id: int, *, merged: bool) -> PullRequest | None:
    async with _session() as s:
        result = await s.execute(
            select(PullRequest)
            .where(PullRequest.github_pr_id == github_pr_id)
            .options(selectinload(PullRequest.slack_channel))
        )
        pr = result.scalar_one_or_none()
        if not pr:
            return None

        now = datetime.now(UTC)
        pr.state = "merged" if merged else "closed"
        if merged:
            pr.merged_at = now
        pr.closed_at = now

        if pr.slack_channel:
            pr.slack_channel.is_archived = True
            pr.slack_channel.archived_at = now

        await s.commit()
        await s.refresh(pr)
        return pr


async def reopen_pr(github_pr_id: int) -> PullRequest | None:
    """Flip a closed/merged PR back to open and unarchive its channel."""
    async with _session() as s:
        result = await s.execute(
            select(PullRequest)
            .where(PullRequest.github_pr_id == github_pr_id)
            .options(selectinload(PullRequest.slack_channel))
        )
        pr = result.scalar_one_or_none()
        if not pr:
            return None

        pr.state = "open"
        pr.closed_at = None
        pr.merged_at = None

        if pr.slack_channel:
            pr.slack_channel.is_archived = False
            pr.slack_channel.archived_at = None

        await s.commit()
        await s.refresh(pr)
        return pr


# ── Thread mapping queries ────────────────────────────────


async def get_thread_mapping(pull_request_id: int, github_thread_id: str) -> ThreadMapping | None:
    async with _session() as s:
        result = await s.execute(
            select(ThreadMapping)
            .where(ThreadMapping.pull_request_id == pull_request_id)
            .where(ThreadMapping.github_thread_id == github_thread_id)
        )
        return result.scalar_one_or_none()


async def get_thread_mapping_by_slack_ts(
    pull_request_id: int, slack_thread_ts: str
) -> ThreadMapping | None:
    async with _session() as s:
        result = await s.execute(
            select(ThreadMapping)
            .where(ThreadMapping.pull_request_id == pull_request_id)
            .where(ThreadMapping.slack_thread_ts == slack_thread_ts)
        )
        return result.scalar_one_or_none()


async def create_thread_mapping(
    *,
    pull_request_id: int,
    github_thread_id: str,
    slack_channel_id: str,
    slack_thread_ts: str,
    file_path: str = "",
) -> ThreadMapping:
    async with _session() as s:
        mapping = ThreadMapping(
            pull_request_id=pull_request_id,
            github_thread_id=github_thread_id,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            file_path=file_path,
        )
        s.add(mapping)
        await s.commit()
        await s.refresh(mapping)
        return mapping


# ── Message mapping queries ───────────────────────────────


async def create_message_mapping(
    *,
    pull_request_id: int,
    slack_channel_id: str,
    slack_ts: str,
    github_comment_id: int,
    github_comment_type: str,
    origin: str,
    slack_user_id: str | None = None,
    slack_ts_extra: list[str] | None = None,
) -> MessageMapping:
    async with _session() as s:
        mapping = MessageMapping(
            pull_request_id=pull_request_id,
            slack_channel_id=slack_channel_id,
            slack_ts=slack_ts,
            github_comment_id=github_comment_id,
            github_comment_type=github_comment_type,
            origin=origin,
            slack_user_id=slack_user_id,
            slack_ts_extra=slack_ts_extra or None,
        )
        s.add(mapping)
        await s.commit()
        await s.refresh(mapping)
        return mapping


async def set_message_mapping_extra_ts(mapping_id: int, slack_ts_extra: list[str] | None) -> None:
    """Replace a mapping's continuation ts list (after an edit changes how many
    messages the comment spans)."""
    async with _session() as s:
        mapping = await s.get(MessageMapping, mapping_id)
        if mapping:
            mapping.slack_ts_extra = slack_ts_extra or None
            await s.commit()


async def get_message_mapping_by_slack_ts(
    pull_request_id: int, slack_ts: str
) -> MessageMapping | None:
    async with _session() as s:
        result = await s.execute(
            select(MessageMapping)
            .where(MessageMapping.pull_request_id == pull_request_id)
            .where(MessageMapping.slack_ts == slack_ts)
        )
        return result.scalar_one_or_none()


async def get_message_mapping_by_github_comment(
    github_comment_id: int, github_comment_type: str
) -> MessageMapping | None:
    async with _session() as s:
        result = await s.execute(
            select(MessageMapping)
            .where(MessageMapping.github_comment_id == github_comment_id)
            .where(MessageMapping.github_comment_type == github_comment_type)
        )
        return result.scalar_one_or_none()


async def delete_message_mapping(mapping_id: int) -> None:
    async with _session() as s:
        mapping = await s.get(MessageMapping, mapping_id)
        if mapping:
            await s.delete(mapping)
            await s.commit()


async def update_pr_state(github_pr_id: int, **fields: object) -> PullRequest | None:
    async with _session() as s:
        pr = await s.get(PullRequest, github_pr_id)
        if not pr:
            result = await s.execute(
                select(PullRequest).where(PullRequest.github_pr_id == github_pr_id)
            )
            pr = result.scalar_one_or_none()
        if not pr:
            return None
        for k, v in fields.items():
            setattr(pr, k, v)
        await s.commit()
        await s.refresh(pr)
        return pr


# ── Scheduler run guard ───────────────────────────────────


async def claim_scheduled_run(job_name: str, scheduled_for: datetime) -> bool:
    """Claim a (job_name, scheduled_for) slot, returning True iff we won the race.

    INSERT ... ON CONFLICT DO NOTHING against the unique constraint: exactly one
    caller inserts the row and gets True; concurrent instances or overlapping cron
    retries collide on the constraint and get False. The caller proceeds only on
    True — this is the at-most-once claim.
    """
    async with _session() as s:
        result = await s.execute(
            pg_insert(SchedulerRun)
            .values(job_name=job_name, scheduled_for=scheduled_for)
            .on_conflict_do_nothing(constraint="uq_scheduler_runs_job")
        )
        await s.commit()
        return result.rowcount == 1


# ── Slack event dedupe ────────────────────────────────────


async def claim_slack_event(event_id: str) -> bool:
    """Claim a Slack event delivery, returning True iff we won the race.

    Slack redelivers events that aren't acked within 3 seconds, so the same
    event_id can arrive multiple times, including while the original delivery
    is still processing. Only the claim winner processes the event.
    """
    async with _session() as s:
        result = await s.execute(
            pg_insert(SlackEventClaim)
            .values(event_id=event_id)
            .on_conflict_do_nothing(constraint="uq_slack_event_claims_event_id")
        )
        await s.commit()
        return result.rowcount == 1


async def release_slack_event(event_id: str) -> None:
    """Release a claim after failed processing so a Slack retry can rescue it."""
    async with _session() as s:
        await s.execute(delete(SlackEventClaim).where(SlackEventClaim.event_id == event_id))
        await s.commit()
