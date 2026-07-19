"""Slack Web API wrapper — channel management, messaging, reactions."""

import logging
from typing import Any

import slack_sdk.errors
from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncRateLimitErrorRetryHandler,
    async_default_handlers,
)
from slack_sdk.web.async_client import AsyncWebClient

from src.config import settings
from src.utils.markdown import SLACK_TEXT_LIMIT, split_for_slack

logger = logging.getLogger(__name__)


class _BodyRateLimitRetryHandler(AsyncRateLimitErrorRetryHandler):
    """Retry rate limits Slack reports in the response body, not just via 429.

    Some methods (e.g. ``bookmarks.add``) signal throttling with an HTTP 200 and
    an ``{"ok": false, "error": "ratelimited"|"too_many_requests"}`` body instead
    of an HTTP 429. The stock handler only retries on 429, so those otherwise
    surface as an unhandled ``SlackApiError``. Backoff (Retry-After, or ~1s when
    the header is absent) is inherited from the parent.
    """

    _RATE_LIMITED_ERRORS = frozenset({"ratelimited", "too_many_requests"})

    async def _can_retry_async(self, *, state, request, response=None, error=None) -> bool:
        if await super()._can_retry_async(
            state=state, request=request, response=response, error=error
        ):
            return True
        if response is None or response.body is None:
            return False
        return response.body.get("error") in self._RATE_LIMITED_ERRORS


def _client(token: str | None = None) -> AsyncWebClient:
    # A long comment is posted as several threaded messages; honor Slack's
    # rate-limit signals (429 header or body error) so those bursts don't drop
    # chunks or crash a handler mid-flow.
    return AsyncWebClient(
        token=token or settings.slack_bot_token,
        retry_handlers=[
            *async_default_handlers(),
            _BodyRateLimitRetryHandler(max_retry_count=3),
        ],
    )


# ── Channels ──────────────────────────────────────────────


async def create_channel(name: str, *, token: str | None = None) -> dict:
    """Create a public Slack channel. Returns the channel object."""
    resp = await _client(token).conversations_create(name=name)
    return resp["channel"]


async def find_channel_by_name(name: str, *, token: str | None = None) -> dict | None:
    """Return the public channel with this exact name, or None if absent.

    `conversations.create` raises `name_taken` when a channel already exists and
    Slack offers no name→id lookup, so resolving the existing channel means
    paging `conversations.list`. Archived channels are included, since a closed
    PR's channel is archived rather than deleted.
    """
    client = _client(token)
    cursor = ""
    while True:
        resp = await client.conversations_list(
            types="public_channel",
            exclude_archived=False,
            limit=1000,
            cursor=cursor,
        )
        for channel in resp["channels"]:
            if channel["name"] == name:
                return channel
        metadata = resp.get("response_metadata")
        cursor = metadata.get("next_cursor") if metadata else None
        if not cursor:
            return None


async def archive_channel(channel_id: str, *, token: str | None = None) -> None:
    await _client(token).conversations_archive(channel=channel_id)


async def unarchive_channel(channel_id: str, *, token: str | None = None) -> None:
    await _client(token).conversations_unarchive(channel=channel_id)


async def invite_to_channel(
    channel_id: str, user_ids: list[str], *, token: str | None = None
) -> None:
    if not user_ids:
        return
    try:
        await _client(token).conversations_invite(channel=channel_id, users=",".join(user_ids))
    except slack_sdk.errors.SlackApiError as e:
        # Re-requesting a review invites a reviewer who's already in the channel;
        # Slack rejects the whole call with already_in_channel. The invariant we
        # want — the user is a member — already holds, so treat it as a no-op.
        if e.response.get("error") != "already_in_channel":
            raise


async def set_channel_topic(channel_id: str, topic: str, *, token: str | None = None) -> None:
    await _client(token).conversations_setTopic(channel=channel_id, topic=topic)


async def add_bookmark(
    channel_id: str,
    title: str,
    link: str,
    *,
    emoji: str | None = None,
    token: str | None = None,
) -> dict:
    kwargs: dict[str, Any] = {
        "channel_id": channel_id,
        "title": title,
        "type": "link",
        "link": link,
    }
    if emoji:
        kwargs["emoji"] = emoji
    resp = await _client(token).bookmarks_add(**kwargs)
    return resp["bookmark"]


async def edit_bookmark(
    channel_id: str,
    bookmark_id: str,
    *,
    title: str | None = None,
    link: str | None = None,
    emoji: str | None = None,
    token: str | None = None,
) -> dict:
    kwargs: dict[str, Any] = {"channel_id": channel_id, "bookmark_id": bookmark_id}
    if title is not None:
        kwargs["title"] = title
    if link is not None:
        kwargs["link"] = link
    if emoji is not None:
        kwargs["emoji"] = emoji
    resp = await _client(token).bookmarks_edit(**kwargs)
    return resp["bookmark"]


# ── Messaging ─────────────────────────────────────────────


async def post_chunked_message(
    channel_id: str,
    text: str,
    *,
    thread_ts: str | None = None,
    username: str | None = None,
    icon_url: str | None = None,
    as_user: bool = False,
    token: str | None = None,
) -> list[dict]:
    """Post `text`, splitting it across messages if it exceeds Slack's limit.

    Slack only renders the first ~4000 characters of a message as one unit and
    splits the remainder into separate messages at raw line boundaries — which
    severs fenced code blocks (a ``` opener strands in one message, its closer in
    another). We split it ourselves on boundaries that keep each code fence
    balanced and post the continuations as replies threaded under the first
    message. Returns one response per message posted, first (the parent) first.
    """
    client = _client(token)
    parts = split_for_slack(text) if len(text) > SLACK_TEXT_LIMIT else [text]
    sent: list[dict] = []
    reply_to = thread_ts
    for part in parts:
        kwargs: dict[str, Any] = {"channel": channel_id, "text": part}
        if as_user:
            kwargs["as_user"] = True
        if username:
            kwargs["username"] = username
        if icon_url:
            kwargs["icon_url"] = icon_url
        if reply_to:
            kwargs["thread_ts"] = reply_to
        resp = await client.chat_postMessage(**kwargs)
        sent.append(resp.data)
        reply_to = reply_to or resp.data.get("ts")
    return sent


async def post_message(
    channel_id: str,
    text: str | None = None,
    *,
    blocks: list[dict] | None = None,
    attachments: list[dict] | None = None,
    thread_ts: str | None = None,
    username: str | None = None,
    icon_url: str | None = None,
    token: str | None = None,
) -> dict:
    # Plain text messages route through the chunker (a long one would otherwise
    # be split by Slack mid-code-block); return the parent message for callers
    # that only track a single ts.
    if text is not None and not blocks and not attachments:
        sent = await post_chunked_message(
            channel_id,
            text,
            thread_ts=thread_ts,
            username=username,
            icon_url=icon_url,
            token=token,
        )
        return sent[0]

    kwargs: dict[str, Any] = {"channel": channel_id}
    if text is not None:
        kwargs["text"] = text
    if blocks:
        kwargs["blocks"] = blocks
    if attachments:
        kwargs["attachments"] = attachments
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if username:
        kwargs["username"] = username
    if icon_url:
        kwargs["icon_url"] = icon_url

    resp = await _client(token).chat_postMessage(**kwargs)
    return resp.data


async def update_message(
    channel_id: str,
    ts: str,
    *,
    text: str | None = None,
    blocks: list[dict] | None = None,
    attachments: list[dict] | None = None,
    token: str | None = None,
) -> dict:
    # Per Slack chat.update docs: passing `text` is what flips the "(edited)"
    # indicator. Update callers that only want to refresh presentation should
    # pass attachments/blocks and leave text out.
    kwargs: dict[str, Any] = {"channel": channel_id, "ts": ts}
    if text is not None:
        kwargs["text"] = text
    if blocks is not None:
        kwargs["blocks"] = blocks
    if attachments is not None:
        kwargs["attachments"] = attachments
    resp = await _client(token).chat_update(**kwargs)
    return resp.data


async def delete_message(channel_id: str, ts: str, *, token: str | None = None) -> dict:
    """Delete a message. The token must match the author: a user-posted message
    can only be deleted with that user's token; a bot message with the bot token.
    """
    resp = await _client(token).chat_delete(channel=channel_id, ts=ts)
    return resp.data


async def post_message_as_user(
    channel_id: str,
    text: str,
    user_token: str,
    *,
    thread_ts: str | None = None,
) -> dict:
    """Post a message using a user's own OAuth token (appears as them).

    Long bodies are split on code-fence-safe boundaries (see post_chunked_message)
    and posted as threaded continuations.
    """
    sent = await post_chunked_message(
        channel_id, text, thread_ts=thread_ts, as_user=True, token=user_token
    )
    return sent[0]


# ── Reactions ─────────────────────────────────────────────


async def add_reaction(
    channel_id: str, timestamp: str, emoji: str, *, token: str | None = None
) -> None:
    await _client(token).reactions_add(channel=channel_id, timestamp=timestamp, name=emoji)


# ── Users ─────────────────────────────────────────────────


async def get_user_info(user_id: str, *, token: str | None = None) -> dict:
    resp = await _client(token).users_info(user=user_id)
    return resp["user"]


async def publish_home(user_id: str, blocks: list[dict], *, token: str | None = None) -> None:
    """Publish or update a user's App Home tab."""
    await _client(token).views_publish(
        user_id=user_id,
        view={"type": "home", "blocks": blocks},
    )


async def lookup_user_by_email(email: str, *, token: str | None = None) -> dict | None:
    try:
        resp = await _client(token).users_lookupByEmail(email=email)
        return resp["user"]
    except slack_sdk.errors.SlackApiError:
        return None
