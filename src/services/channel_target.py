"""Which PR a Slack action in a PR channel is about.

A PR channel normally belongs to one PR. With STACK_CHANNELS it can hold a
stack: the PR that owns the channel plus stacked PRs, each with its own parent
message and thread. The owner can also close while stacked PRs stay open.
"""

from dataclasses import dataclass, field

from src.db.queries import (
    get_message_mapping_in_channel,
    get_open_prs_in_channel,
    get_pr_by_channel,
    get_pr_by_channel_thread,
    get_pr_by_id,
    get_pr_in_channel_by_number,
)
from src.models.pull_request import PullRequest


@dataclass
class ChannelTarget:
    # The PR the action is about, or None when the channel has no PR or the
    # action is ambiguous (see `open_prs`).
    pr: PullRequest | None
    # The channel's open PRs. With `pr` None and more than one entry here, the
    # caller asks the user to name a PR.
    open_prs: list[PullRequest] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return self.pr is None and len(self.open_prs) > 1


async def _pr_for_message(channel_id: str, ts: str) -> PullRequest | None:
    """The PR a given message belongs to: a stacked PR's parent message, or a
    message Pulley mapped to a GitHub comment."""
    stacked = await get_pr_by_channel_thread(channel_id, ts)
    if stacked:
        return stacked
    mapping = await get_message_mapping_in_channel(channel_id, ts)
    if mapping:
        return await get_pr_by_id(mapping.pull_request_id)
    return None


async def resolve_channel_pr(
    channel_id: str,
    *,
    message_ts: str | None = None,
    pr_number: int | None = None,
    strict: bool = False,
) -> ChannelTarget:
    """Resolve the PR an action in `channel_id` is about.

    1. `pr_number`, when given, names the PR (any state) among the channel's PRs.
    2. `message_ts` (a thread root, or a reacted message) resolves to the PR that
       message belongs to.
    3. Otherwise the action is about the channel as a whole. That is the owner
       while it is open, else the channel's only open PR. With `strict` (merge,
       approve), more than one open PR is ambiguous even when the owner is open.
    """
    if pr_number is not None:
        return ChannelTarget(await get_pr_in_channel_by_number(channel_id, pr_number))

    if message_ts:
        pr = await _pr_for_message(channel_id, message_ts)
        if pr:
            return ChannelTarget(pr)

    owner = await get_pr_by_channel(channel_id)
    open_prs = await get_open_prs_in_channel(channel_id)
    if len(open_prs) == 1:
        return ChannelTarget(open_prs[0], open_prs)
    if not open_prs:
        # Everything in the channel is closed: the channel is about its owner.
        return ChannelTarget(owner, open_prs)
    if not strict and owner and owner.state == "open":
        return ChannelTarget(owner, open_prs)
    return ChannelTarget(None, open_prs)


def ambiguity_hint(target: ChannelTarget, usage: str) -> str:
    numbers = ", ".join(f"#{p.github_pr_number}" for p in target.open_prs)
    return f"This channel holds several open PRs ({numbers}). Name one: `{usage}`"
