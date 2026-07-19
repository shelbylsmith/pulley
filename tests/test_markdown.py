from src.utils.markdown import (
    SLACK_TEXT_LIMIT,
    blockquote,
    gfm_to_slack,
    github_mention_logins,
    slack_mention_ids,
    slack_to_gfm,
    split_for_slack,
)


def _fence_count(chunk: str) -> int:
    return sum(1 for ln in chunk.split("\n") if ln.startswith("```"))


def _content(text: str) -> str:
    """Concatenate every non-fence line — what should survive a round trip
    regardless of where chunk boundaries (and the fences we add at them) fall."""
    return "".join(ln for ln in text.split("\n") if ln != "```")


class TestGfmToSlack:
    def test_empty(self) -> None:
        assert gfm_to_slack("") == ""

    def test_bold_double_star(self) -> None:
        assert gfm_to_slack("hello **world** foo") == "hello *world* foo"

    def test_bold_double_underscore(self) -> None:
        assert gfm_to_slack("hello __world__") == "hello *world*"

    def test_italic_single_star(self) -> None:
        assert gfm_to_slack("hello *world* foo") == "hello _world_ foo"

    def test_italic_underscore_unchanged(self) -> None:
        assert gfm_to_slack("hello _world_") == "hello _world_"

    def test_bold_then_italic_not_confused(self) -> None:
        assert gfm_to_slack("**bold** and *italic*") == "*bold* and _italic_"

    def test_strikethrough(self) -> None:
        assert gfm_to_slack("~~gone~~") == "~gone~"

    def test_link(self) -> None:
        assert gfm_to_slack("see [docs](https://x.com)") == "see <https://x.com|docs>"

    def test_image(self) -> None:
        assert gfm_to_slack("![alt](https://x.com/i.png)") == "<https://x.com/i.png|alt>"

    def test_header_h1(self) -> None:
        assert gfm_to_slack("# Title") == "*Title*"

    def test_header_h3(self) -> None:
        assert gfm_to_slack("### Section") == "*Section*"

    def test_bullet_dash(self) -> None:
        assert gfm_to_slack("- one\n- two") == "• one\n• two"

    def test_bullet_star(self) -> None:
        assert gfm_to_slack("* one\n* two") == "• one\n• two"

    def test_code_block_unchanged_contents(self) -> None:
        out = gfm_to_slack("before\n\n```\n**not bold**\n```\n\nafter")
        assert "**not bold**" in out
        assert "```\n**not bold**\n```" in out

    def test_inline_code_preserved(self) -> None:
        assert gfm_to_slack("use `**foo**` literally") == "use `**foo**` literally"

    def test_multiline_body(self) -> None:
        src = "## Summary\n\n**Important:** fix [bug](https://x/1).\n\n- item one\n- item two"
        expected = "*Summary*\n\n*Important:* fix <https://x/1|bug>.\n\n• item one\n• item two"
        assert gfm_to_slack(src) == expected

    # ── Adversarial cases the regex version got wrong ──

    def test_triple_star_bold_italic(self) -> None:
        # ***x*** is bold+italic in GFM. Slack supports nesting → _*x*_.
        assert gfm_to_slack("***bold italic***") == "_*bold italic*_"

    def test_escaped_asterisks(self) -> None:
        assert gfm_to_slack(r"literal \*not bold\*") == "literal *not bold*"

    def test_escaped_underscores(self) -> None:
        assert gfm_to_slack(r"literal \_not italic\_") == "literal _not italic_"

    def test_snake_case_unaffected(self) -> None:
        assert gfm_to_slack("use snake_case_name here") == "use snake_case_name here"

    def test_nested_bold_in_italic(self) -> None:
        assert gfm_to_slack("*italic **bold** italic*") == "_italic *bold* italic_"

    def test_nested_italic_in_bold(self) -> None:
        assert gfm_to_slack("**bold _italic_ bold**") == "*bold _italic_ bold*"

    def test_unbalanced_delimiter_left_literal(self) -> None:
        assert gfm_to_slack("**oops") == "**oops"

    def test_task_list_renders_with_checkbox_glyph(self) -> None:
        out = gfm_to_slack("- [ ] todo\n- [x] done")
        assert "☐ todo" in out
        assert "☑ done" in out

    def test_nested_list_indents(self) -> None:
        out = gfm_to_slack("- one\n- two\n  - nested")
        assert "• one" in out
        assert "• two" in out
        assert "    • nested" in out

    def test_ordered_list(self) -> None:
        assert gfm_to_slack("1. first\n2. second") == "1. first\n2. second"

    def test_blockquote(self) -> None:
        out = gfm_to_slack("> quoted line\n> more quote")
        assert "> quoted line" in out
        assert "> more quote" in out

    def test_thematic_break(self) -> None:
        assert "─" in gfm_to_slack("---")

    def test_autolink_url(self) -> None:
        # GFM auto-links bare URLs (url plugin) — Slack wraps them in <>.
        out = gfm_to_slack("see https://x.com please")
        assert "<https://x.com>" in out

    def test_codespan_keeps_inner_markdown_literal(self) -> None:
        assert gfm_to_slack("call `f(**kwargs)` here") == "call `f(**kwargs)` here"

    def test_link_inside_bold(self) -> None:
        out = gfm_to_slack("**see [docs](https://x.com)**")
        assert out == "*see <https://x.com|docs>*"

    def test_github_at_mention_left_literal_when_unmapped(self) -> None:
        assert gfm_to_slack("cc @octocat please") == "cc @octocat please"

    def test_issue_ref_left_literal(self) -> None:
        assert gfm_to_slack("fixes #123") == "fixes #123"

    def test_code_block_preserves_internal_blank_line(self) -> None:
        assert gfm_to_slack("```\nfoo\n\nbar\n```") == "```\nfoo\n\nbar\n```"

    def test_code_block_inside_list_is_on_new_line(self) -> None:
        out = gfm_to_slack("- item\n  ```\n  code\n  ```")
        assert out == "• item\n```\ncode\n```"

    def test_code_block_round_trip_simple(self) -> None:
        assert gfm_to_slack("```\nx = 1\n```") == "```\nx = 1\n```"

    def test_code_block_drops_language_hint(self) -> None:
        # Slack doesn't render the language hint, so showing it as the first
        # line of the block (Slack's literal interpretation) would be ugly.
        assert gfm_to_slack("```python\nimport x\n```") == "```\nimport x\n```"

    def test_indented_code_block(self) -> None:
        assert gfm_to_slack("    foo\n    bar") == "```\nfoo\nbar\n```"

    def test_img_tag_becomes_link(self) -> None:
        # GitHub rewrites pasted images to <img src=... alt=...>; Slack has no
        # HTML, so it must render as a link, not leak the raw tag (and its jwt).
        src = "https://private-user-images.githubusercontent.com/x.svg?jwt=abc"
        out = gfm_to_slack(f'look <img alt="meme" width="800" src="{src}">')
        assert out == f"look <{src}|meme>"

    def test_img_tag_without_alt(self) -> None:
        out = gfm_to_slack('<img src="https://e.com/x.png">')
        assert out == "<https://e.com/x.png>"

    def test_details_summary_flattened(self) -> None:
        # <details>/<summary> have no Slack equivalent: drop the tags, bold the
        # summary label, keep the inner content.
        out = gfm_to_slack(
            "<details><summary>Show Output</summary>\n\n```\nplan\n```\n\n</details>"
        )
        assert "<details>" not in out and "</summary>" not in out
        assert "*Show Output*" in out
        assert "```\nplan\n```" in out

    def test_paragraph_after_code_block_in_list_item(self) -> None:
        # Loose list item: paragraph → code block → paragraph. The trailing
        # paragraph must not glue onto the code block's closing fence.
        out = gfm_to_slack("* run:\n  ```\n  cmd\n  ```\nPlan: done")
        assert out == "• run:\n```\ncmd\n```\nPlan: done"

    def test_emoji_shortcode_to_unicode(self) -> None:
        assert gfm_to_slack("ship it :rocket: :+1:") == "ship it 🚀 👍"

    def test_unknown_emoji_shortcode_left_literal(self) -> None:
        assert gfm_to_slack("custom :not_an_emoji: here") == "custom :not_an_emoji: here"

    def test_emoji_in_code_untouched(self) -> None:
        assert gfm_to_slack("`:rocket:` stays") == "`:rocket:` stays"


class TestMentions:
    MAP = {"octocat": "U123", "foo-bar": "U999"}

    def test_mapped_mention_becomes_slack_ping(self) -> None:
        assert gfm_to_slack("cc @octocat please", self.MAP) == "cc <@U123> please"

    def test_unmapped_mention_stays_literal(self) -> None:
        assert gfm_to_slack("cc @nobody please", self.MAP) == "cc @nobody please"

    def test_mention_lookup_is_case_insensitive(self) -> None:
        assert gfm_to_slack("hi @OctoCat", self.MAP) == "hi <@U123>"

    def test_mention_nested_in_bold(self) -> None:
        assert gfm_to_slack("**@octocat** look", self.MAP) == "*<@U123>* look"

    def test_mention_in_code_span_untouched(self) -> None:
        assert gfm_to_slack("use `@octocat` here", self.MAP) == "use `@octocat` here"

    def test_email_is_not_a_mention(self) -> None:
        assert gfm_to_slack("mail me@example.com", self.MAP) == "mail me@example.com"

    def test_logins_extracted_skipping_code(self) -> None:
        logins = github_mention_logins("hi @octocat `@incode` me@x.com **@foo-bar**")
        assert logins == {"octocat", "foo-bar"}

    def test_logins_empty_when_none(self) -> None:
        assert github_mention_logins("no mentions here") == set()


class TestBlockquote:
    def test_quotes_each_line(self) -> None:
        assert blockquote("a\nb") == "> a\n> b"

    def test_blank_line_gets_bare_marker(self) -> None:
        assert blockquote("a\n\nb") == "> a\n>\n> b"

    def test_fenced_code_left_unquoted(self) -> None:
        # Slack won't render a ``` block whose fence line is quoted, so code
        # blocks pass through unquoted while surrounding prose stays quoted.
        out = blockquote("before\n```\ncode\n```\nafter")
        assert out == "> before\n```\ncode\n```\n> after"


class TestSlackToGfm:
    def test_empty(self) -> None:
        assert slack_to_gfm("") == ""

    def test_bold(self) -> None:
        assert slack_to_gfm("hello *world*") == "hello **world**"

    def test_italic_unchanged(self) -> None:
        assert slack_to_gfm("hello _world_") == "hello _world_"

    def test_strike(self) -> None:
        assert slack_to_gfm("~gone~") == "~~gone~~"

    def test_link(self) -> None:
        assert slack_to_gfm("see <https://x.com|docs>") == "see [docs](https://x.com)"

    def test_bare_url_unchanged(self) -> None:
        assert slack_to_gfm("<https://x.com>") == "<https://x.com>"

    def test_channel_mention(self) -> None:
        assert slack_to_gfm("in <#C123|general>") == "in #general"

    def test_code_block_preserved(self) -> None:
        src = "```\n*not bold*\n```"
        assert slack_to_gfm(src) == src

    def test_inline_code_preserved(self) -> None:
        assert slack_to_gfm("use `*foo*` literal") == "use `*foo*` literal"

    def test_user_mention_resolved(self) -> None:
        out = slack_to_gfm("hey <@U123>", {"U123": "@octocat"})
        assert out == "hey @octocat"

    def test_user_mention_with_inline_name_uses_map(self) -> None:
        out = slack_to_gfm("hey <@U123|display>", {"U123": "@octocat"})
        assert out == "hey @octocat"

    def test_unmapped_user_mention_left_raw(self) -> None:
        assert slack_to_gfm("hey <@U000>", {"U123": "@octocat"}) == "hey <@U000>"

    def test_user_mention_in_code_untouched(self) -> None:
        assert slack_to_gfm("`<@U123>` stays", {"U123": "@octocat"}) == "`<@U123>` stays"

    def test_no_mentions_map_leaves_mention_raw(self) -> None:
        assert slack_to_gfm("hey <@U123>") == "hey <@U123>"


class TestSlackMentionIds:
    def test_extracts_ids(self) -> None:
        assert slack_mention_ids("a <@U123> and <@U999|name>") == {"U123", "U999"}

    def test_skips_code(self) -> None:
        assert slack_mention_ids("real <@U123> `<@UCODE>`") == {"U123"}

    def test_empty(self) -> None:
        assert slack_mention_ids("no mentions") == set()


class TestSplitForSlack:
    def test_short_text_is_single_chunk(self) -> None:
        assert split_for_slack("hello world") == ["hello world"]

    def test_text_at_limit_is_not_split(self) -> None:
        assert split_for_slack("a" * SLACK_TEXT_LIMIT) == ["a" * SLACK_TEXT_LIMIT]

    def test_every_chunk_within_limit(self) -> None:
        text = "\n".join(f"line {i} of some output" for i in range(1000))
        assert all(len(c) <= SLACK_TEXT_LIMIT for c in split_for_slack(text))

    def test_fence_spanning_boundary_is_balanced_in_each_chunk(self) -> None:
        # A single code block far longer than one chunk: every chunk must carry a
        # balanced pair of fences so Slack renders each as its own code box.
        code = "\n".join(f"output row {i} with content" for i in range(400))
        chunks = split_for_slack(f"```\n{code}\n```")
        assert len(chunks) > 1
        assert all(_fence_count(c) % 2 == 0 for c in chunks)
        assert all(c.startswith("```") and c.rstrip().endswith("```") for c in chunks)

    def test_content_preserved_across_split(self) -> None:
        code = "\n".join(f"row {i}" for i in range(2000))
        original = f"intro\n```\n{code}\n```\noutro"
        assert _content("\n".join(split_for_slack(original))) == _content(original)

    def test_no_synthetic_fences_when_no_code_block(self) -> None:
        text = "\n".join(f"> quoted prose line {i}" for i in range(400))
        chunks = split_for_slack(text)
        assert len(chunks) > 1
        assert all(_fence_count(c) == 0 for c in chunks)

    def test_oversized_single_line_is_hard_split(self) -> None:
        chunks = split_for_slack("x" * (SLACK_TEXT_LIMIT * 2 + 50))
        assert all(len(c) <= SLACK_TEXT_LIMIT for c in chunks)
        assert "".join(chunks) == "x" * (SLACK_TEXT_LIMIT * 2 + 50)
