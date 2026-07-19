from sqlalchemy import JSON, BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.database import Base


class MessageMapping(Base):
    """Maps a single synced comment to its mirror message on the other platform.

    One row per synced item, in either direction:

    - ``origin="github"`` — a GitHub comment/review that we posted into Slack.
      Used to mirror GitHub edits/deletes onto the Slack message.
    - ``origin="slack"`` — a Slack message that we posted to GitHub as a comment.
      Used to mirror Slack edits/deletes onto the GitHub comment.

    ``origin`` is also the echo guard: a sync-induced edit/delete on one side lands
    on a row whose origin is the *other* side, so that side's handler ignores it.
    """

    __tablename__ = "message_mappings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(BigInteger, index=True)

    slack_channel_id: Mapped[str] = mapped_column(String(64))
    # ts of the parent message. A long comment is posted as several messages —
    # the parent in the channel and the rest threaded under it; this stays the
    # parent (also the thread anchor) across edits.
    slack_ts: Mapped[str] = mapped_column(String(64), index=True)
    # ts of the threaded continuation messages, in order, when the comment was
    # too long for one Slack message. None/empty for the common single-message case.
    slack_ts_extra: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    github_comment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # issue_comment | review_comment | review
    github_comment_type: Mapped[str] = mapped_column(String(32))

    # github | slack
    origin: Mapped[str] = mapped_column(String(16))
    # Author's Slack ID, for resolving the token to edit/delete a slack-origin comment.
    slack_user_id: Mapped[str | None] = mapped_column(String(64))
