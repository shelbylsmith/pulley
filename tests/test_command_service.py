"""Tests for /pulley settings: channel parsing and the documented responses."""

from types import SimpleNamespace

import pytest

from src.services import command_service


def make_org(**overrides):
    fields = {
        "id": 1,
        "github_installation_id": None,
        "github_org_login": None,
        "pr_channel_id": None,
        "ci_channel_id": None,
        "issue_channel_id": None,
        "recap_channel_id": None,
        "recap_cron": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def org_and_writes(monkeypatch):
    """Serve one org to _cmd_settings and capture what it writes back."""
    state = SimpleNamespace(org=make_org(), writes=[])

    async def fake_get_org_by_slack_team(team_id):
        return state.org

    async def fake_update_org_settings(org_id, **fields):
        state.writes.append((org_id, fields))

    monkeypatch.setattr(
        "src.db.queries.get_org_by_slack_team", fake_get_org_by_slack_team, raising=True
    )
    monkeypatch.setattr(
        "src.db.queries.update_org_settings", fake_update_org_settings, raising=True
    )
    return state


# ── Channel mention parsing ───────────────────────────────


# Escaped forms Slack documents for slash commands with should_escape on:
# public channels carry the ID and name, private channels carry the ID alone.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<#C123456789|public-channel-01>", "C123456789"),
        ("<#C012ABCDE>", "C012ABCDE"),
        ("<#C987654321|>", "C987654321"),
        ("<C987654321|>", "C987654321"),
    ],
)
def test_parses_escaped_channel_mentions(raw, expected):
    assert command_service._parse_channel_id(raw) == expected


# Unlinkified text is not a channel: storing it would leave the setting looking
# configured while every post to it failed.
@pytest.mark.parametrize("raw", ["#eng-alerts", "eng-alerts", "C123456789", "<#|>", "<>", ""])
def test_rejects_values_slack_did_not_linkify(raw):
    assert command_service._parse_channel_id(raw) is None


# ── Reading settings ──────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_names_every_setting_and_what_it_posts(org_and_writes):
    result = await command_service._cmd_settings("", "T1", "C0")

    text = result["text"]
    assert result["response_type"] == "ephemeral"
    for name in ("pr", "ci", "issues", "recap"):
        assert f"`{name}`" in text
    assert "PR digest" in text
    assert "CI alerts" in text
    assert "Issue cards" in text
    assert "Daily recap" in text
    assert "_not set_" in text


@pytest.mark.asyncio
async def test_overview_shows_effective_recap_schedule(org_and_writes):
    org_and_writes.org = make_org(recap_channel_id="C3", recap_cron="30 8 * * 1-5")

    result = await command_service._cmd_settings("", "T1", "C0")

    assert "30 8 * * 1-5" in result["text"]


@pytest.mark.asyncio
async def test_overview_falls_back_to_configured_recap_cron(org_and_writes, monkeypatch):
    monkeypatch.setattr("src.config.settings.recap_cron", "15 7 * * 1-5", raising=True)

    result = await command_service._cmd_settings("", "T1", "C0")

    assert "15 7 * * 1-5" in result["text"]


@pytest.mark.asyncio
async def test_single_setting_reports_current_channel(org_and_writes):
    org_and_writes.org = make_org(ci_channel_id="C2")

    result = await command_service._cmd_settings("ci", "T1", "C0")

    assert "<#C2>" in result["text"]
    assert org_and_writes.writes == []


@pytest.mark.asyncio
async def test_unknown_setting_lists_the_valid_ones(org_and_writes):
    result = await command_service._cmd_settings("recp #foo", "T1", "C0")

    text = result["text"]
    assert "recp" in text
    assert "`pr`" in text and "`ci`" in text and "`recap`" in text
    assert org_and_writes.writes == []


# ── Writing settings ──────────────────────────────────────


@pytest.mark.asyncio
async def test_setting_a_channel_writes_the_matching_column(org_and_writes):
    result = await command_service._cmd_settings("pr <#C9|eng-prs>", "T1", "C0")

    assert org_and_writes.writes == [(1, {"pr_channel_id": "C9"})]
    assert "<#C9>" in result["text"]


@pytest.mark.asyncio
async def test_each_setting_maps_to_its_own_column(org_and_writes):
    await command_service._cmd_settings("ci <#C1>", "T1", "C0")
    await command_service._cmd_settings("recap <#C2>", "T1", "C0")
    await command_service._cmd_settings("issues <#C3>", "T1", "C0")

    assert org_and_writes.writes == [
        (1, {"ci_channel_id": "C1"}),
        (1, {"recap_channel_id": "C2"}),
        (1, {"issue_channel_id": "C3"}),
    ]


@pytest.mark.asyncio
async def test_unlinkified_channel_is_rejected_without_writing(org_and_writes):
    result = await command_service._cmd_settings("ci #eng-alerts", "T1", "C0")

    assert org_and_writes.writes == []
    assert "#eng-alerts" in result["text"]


@pytest.mark.asyncio
async def test_missing_org_does_not_write(org_and_writes):
    org_and_writes.org = None

    result = await command_service._cmd_settings("pr <#C9|eng-prs>", "T1", "C0")

    assert org_and_writes.writes == []
    assert "not connected" in result["text"].lower()
