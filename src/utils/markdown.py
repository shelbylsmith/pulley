"""Bridge GitHub-flavored Markdown (GFM) and Slack mrkdwn.

GFM → Slack uses mistune's AST and a custom walker, so nested/escaped/edge-case
combinations parse correctly (no regex tape).

Slack → GFM is regex because Slack mrkdwn is a tiny non-CommonMark dialect with
no real parser. Surface area is small (bold/italic/strike/link/mention).
"""

import re
from contextvars import ContextVar
from html.parser import HTMLParser

import emoji
import mistune

# ── GFM → Slack ────────────────────────────────────────────

# GitHub @mention. A login is 1–39 chars, alphanumeric with single internal
# hyphens and no leading/trailing hyphen; the negative lookbehind stops it
# firing inside emails (me@host) or URL paths (/@handle). It's a mistune inline
# rule rather than a regex over rendered output, so the parser — not us —
# decides where it applies: never inside code spans, autolinks, or link
# destinations, and correctly even when nested in bold/italic.
_MENTION_PATTERN = r"(?<![\w/])@(?P<gh_login>[A-Za-z\d](?:[A-Za-z\d-]{0,37}[A-Za-z\d])?)"


def _parse_mention(
    _inline: mistune.InlineParser, m: re.Match[str], state: mistune.InlineState
) -> int:
    state.append_token(
        {"type": "gh_mention", "attrs": {"login": m.group("gh_login")}, "raw": m.group(0)}
    )
    return m.end()


def _mention_plugin(md: mistune.Markdown) -> None:
    md.inline.register("gh_mention", _MENTION_PATTERN, _parse_mention, before="link")


_md = mistune.create_markdown(
    renderer="ast",
    plugins=["strikethrough", "task_lists", "table", "url", _mention_plugin],
)

# Slack <@id> mentions to substitute for GitHub logins during a render, keyed by
# lowercased login. Set per-call by gfm_to_slack; None/empty leaves @login literal.
_mentions: ContextVar[dict[str, str] | None] = ContextVar("gfm_mentions", default=None)


def _emojize(text: str) -> str:
    """Turn GitHub `:shortcode:` emoji into Unicode (▶️, 🎉, …).

    GitHub renders gemoji shortcodes; Slack mrkdwn doesn't, so they'd otherwise
    arrive as literal `:arrow_forward:`. Unicode renders the same everywhere and
    needs no workspace emoji config. Unknown shortcodes are left untouched.
    """
    return emoji.emojize(text, language="alias")


# Slack mrkdwn has no HTML, so raw HTML nodes (GitHub allows a subset: <img>,
# <details>/<summary>, <br>, <sub>, ...) are flattened to text. <img> becomes a
# link (GitHub rewrites pasted images to these), <br> a newline, the <summary>
# label is bolded since it heads an expandable section, and every other tag is
# dropped while keeping its text content.
class _HtmlToSlack(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            a = dict(attrs)
            src = a.get("src")
            if src:
                alt = a.get("alt")
                self.parts.append(f"<{src}|{alt}>" if alt else f"<{src}>")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "summary":
            self.parts.append("*")

    def handle_endtag(self, tag: str) -> None:
        if tag == "summary":
            self.parts.append("*")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_to_slack(raw: str) -> str:
    parser = _HtmlToSlack()
    parser.feed(raw)
    return "".join(parser.parts)


def _render(nodes: list[dict]) -> str:
    return "".join(_render_node(n) for n in nodes)


def _render_inline(nodes: list[dict]) -> str:
    return "".join(_render_node(n) for n in nodes)


def _render_node(node: dict) -> str:
    t = node["type"]
    kids = node.get("children", [])

    # ── Inline ──
    if t == "text":
        return _emojize(node.get("raw", ""))
    if t == "gh_mention":
        sid = (_mentions.get() or {}).get(node["attrs"]["login"].lower())
        return f"<@{sid}>" if sid else node.get("raw", "")
    if t == "strong":
        return f"*{_render_inline(kids)}*"
    if t == "emphasis":
        return f"_{_render_inline(kids)}_"
    if t == "strikethrough":
        return f"~{_render_inline(kids)}~"
    if t == "codespan":
        return f"`{node.get('raw', '')}`"
    if t == "link":
        url = node.get("attrs", {}).get("url", "")
        label = _render_inline(kids)
        if not label or label == url:
            return f"<{url}>"
        return f"<{url}|{label}>"
    if t == "image":
        url = node.get("attrs", {}).get("url", "")
        alt = _render_inline(kids)
        return f"<{url}|{alt}>" if alt else f"<{url}>"
    if t in ("linebreak", "softbreak"):
        return "\n"
    if t == "inline_html":
        return _html_to_slack(node.get("raw", ""))

    # ── Block ──
    if t == "paragraph":
        return _render_inline(kids) + "\n\n"
    if t == "heading":
        return f"*{_render_inline(kids)}*\n\n"
    if t == "block_code":
        # mistune appends exactly one trailing newline to `raw`; keep any
        # additional internal blank lines intact so the user's spacing
        # inside the block is preserved.
        code = node.get("raw", "")
        if not code.endswith("\n"):
            code += "\n"
        return f"```\n{code}```\n\n"
    if t == "block_quote":
        inner = _render(kids).rstrip("\n")
        quoted = "\n".join(f"> {ln}" if ln else ">" for ln in inner.split("\n"))
        return quoted + "\n\n"
    if t == "list":
        return _render_list(node)
    if t == "block_text":
        return _render_inline(kids)
    if t == "thematic_break":
        return "──────────\n\n"
    if t == "block_html":
        return _html_to_slack(node.get("raw", ""))
    if t == "blank_line":
        return ""
    if t == "table":
        return _render_table(node)

    # Unknown node — render children if any, else best-effort raw.
    if kids:
        return _render(kids)
    return node.get("raw", "")


def _render_list(node: dict) -> str:
    ordered = node.get("attrs", {}).get("ordered", False)
    depth = node.get("attrs", {}).get("depth", 0)
    indent = "    " * depth
    out: list[str] = []
    for i, item in enumerate(node.get("children", []), start=1):
        if item.get("type") == "task_list_item":
            checked = item.get("attrs", {}).get("checked", False)
            marker = "☑" if checked else "☐"
        elif ordered:
            marker = f"{i}."
        else:
            marker = "•"
        body = _render_list_item(item.get("children", []))
        out.append(f"{indent}{marker} {body}")
    return "\n".join(out) + "\n\n"


def _render_list_item(children: list[dict]) -> str:
    """A list item holds a block_text plus optional sibling blocks (further
    paragraphs, nested lists, code blocks). Render inline content on the first
    line, then every later child on its own line so none get glued to the
    preceding one (a code block's closing fence, the bullet, etc.).
    """
    parts: list[str] = []
    for i, child in enumerate(children):
        ctype = child.get("type")
        if ctype in ("block_text", "paragraph"):
            body = _render_inline(child.get("children", []))
        else:
            body = _render_node(child).rstrip("\n")
        parts.append(f"\n{body}" if i > 0 else body)
    return "".join(parts).rstrip()


def _render_table(node: dict) -> str:
    """Slack has no tables; render as text rows separated by ' | '."""
    lines: list[str] = []
    for section in node.get("children", []):
        if section.get("type") == "table_head":
            cells = [_render_inline(c.get("children", [])) for c in section.get("children", [])]
            lines.append(" | ".join(cells))
            lines.append("─" * max(8, sum(len(c) for c in cells) + 3 * len(cells)))
        elif section.get("type") == "table_body":
            for row in section.get("children", []):
                cells = [_render_inline(c.get("children", [])) for c in row.get("children", [])]
                lines.append(" | ".join(cells))
    return "\n".join(lines) + "\n\n"


def gfm_to_slack(text: str, mentions: dict[str, str] | None = None) -> str:
    """Convert GitHub-flavored Markdown to Slack mrkdwn.

    `mentions` maps lowercased GitHub logins to Slack user IDs; matching
    @mentions become `<@id>` pings. Unmapped logins stay literal `@login`.
    """
    if not text:
        return text
    token = _mentions.set(mentions or {})
    try:
        return _render(_md(text)).rstrip("\n")
    finally:
        _mentions.reset(token)


def github_mention_logins(text: str) -> set[str]:
    """GitHub logins @-mentioned in `text`, per the parser (skips code/links)."""
    logins: set[str] = set()

    def collect(nodes: list[dict]) -> None:
        for n in nodes:
            if n.get("type") == "gh_mention":
                logins.add(n["attrs"]["login"])
            collect(n.get("children", []))

    collect(_md(text))
    return logins


def blockquote(text: str) -> str:
    """Prefix every line of Slack mrkdwn with '> ' to quote it.

    Lines inside fenced code blocks are left unquoted: Slack won't render a
    ``` block whose fence line is itself quoted, so quoting only the first line
    (the old behavior) broke multi-line bodies — code blocks especially. Code
    blocks render on their own; surrounding prose stays quoted.
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            out.append(line)
        elif in_fence:
            out.append(line)
        else:
            out.append(f"> {line}" if line else ">")
    return "\n".join(out)


# ── Chunking for Slack's per-message size limit ────────────

# Slack splits any single message longer than ~4000 characters at raw line
# boundaries, with no regard for fenced code blocks — so a ``` opener strands in
# one message and its closer in the next, and the block renders as literal text.
# We chunk below that ourselves, leaving headroom for the synthetic fences we add
# when a code block spans a boundary.
SLACK_TEXT_LIMIT = 3800
_FENCE = "```"


def split_for_slack(text: str, limit: int = SLACK_TEXT_LIMIT) -> list[str]:
    """Split Slack mrkdwn into messages of at most `limit` characters.

    Breaks only at newlines. When a chunk boundary falls inside a fenced code
    block, the open fence is closed at the end of that chunk and reopened at the
    start of the next, so every message is self-contained and renders its code as
    a real code block. A single line longer than `limit` is hard-split.
    """
    if len(text) <= limit:
        return [text]

    # A single line longer than a chunk can never fit on its own, so hard-wrap
    # those first (leaving room for the fences/newline we may add around a
    # chunk). Rare for real comment text, but a lone giant line would otherwise
    # force an over-limit chunk.
    maxline = limit - 2 * (len(_FENCE) + 1)
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > maxline:
            lines.append(line[:maxline])
            line = line[maxline:]
        lines.append(line)

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0  # == len("\n".join(cur))
    in_fence = False  # fence state after the lines currently in `cur`

    def flush() -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        body = "\n".join(cur)
        if in_fence:  # mid-code-block: close it here, reopen in the next chunk
            body += f"\n{_FENCE}"
        chunks.append(body)
        cur, cur_len = ([_FENCE], len(_FENCE)) if in_fence else ([], 0)

    for line in lines:
        next_fence = in_fence ^ line.startswith(_FENCE)
        cost = len(line) + (1 if cur else 0)
        # If this chunk would end inside a block, reserve room for its closer.
        reserve = len(_FENCE) + 1 if next_fence else 0
        if cur and cur_len + cost + reserve > limit:
            flush()
            cost = len(line) + (1 if cur else 0)
        cur.append(line)
        cur_len += cost
        in_fence = next_fence

    if cur:
        body = "\n".join(cur)
        if in_fence:  # defensive: malformed input left a fence open
            body += f"\n{_FENCE}"
        chunks.append(body)
    return chunks


# ── Slack → GFM ────────────────────────────────────────────

# Code spans (fenced ``` and inline ``) — protected from rewrites.
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Slack user mention: <@U123> or <@U123|display-name>.
_SLACK_MENTION_RE = re.compile(r"<@(?P<id>[A-Z0-9]+)(?:\|[^>]+)?>")


def slack_mention_ids(text: str) -> set[str]:
    """Slack user IDs @-mentioned in `text`, ignoring code spans."""
    stripped = _INLINE_CODE_RE.sub("", _FENCED_CODE_RE.sub("", text))
    return {m.group("id") for m in _SLACK_MENTION_RE.finditer(stripped)}


def slack_to_gfm(text: str, mentions: dict[str, str] | None = None) -> str:
    """Convert Slack mrkdwn to GitHub-flavored Markdown.

    `mentions` maps a Slack user ID to the text that replaces its `<@id>`
    mention (e.g. `@github-login` for a linked user, or a display name for an
    unlinked one). Unlisted IDs are left as their raw `<@id>`.
    """
    if not text:
        return text

    saved: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        saved.append(m.group(0))
        return f"\x00P{len(saved) - 1}\x00"

    text = _FENCED_CODE_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)

    # User mentions <@U123> / <@U123|name> → caller-supplied replacement.
    if mentions:
        text = _SLACK_MENTION_RE.sub(lambda m: mentions.get(m.group("id"), m.group(0)), text)
    # Channel mentions <#C123|name> → #name
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)
    # Labeled links <url|label> → [label](url)
    text = re.sub(
        r"<((?:https?|mailto)[^|>\s]+)\|([^>]+)>",
        r"[\2](\1)",
        text,
    )
    # Bare <url> → leave; GFM treats this as an autolink.

    # *bold* → **bold**
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"**\1**", text)
    # ~strike~ → ~~strike~~
    text = re.sub(r"(?<![\w~])~([^~\n]+?)~(?![\w~])", r"~~\1~~", text)
    # _italic_ stays the same in both.

    for i, span in enumerate(saved):
        text = text.replace(f"\x00P{i}\x00", span)
    return text
