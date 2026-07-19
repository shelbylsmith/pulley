import pytest
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.main import app
from src.routers import github_webhooks


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def stub_signature_and_dispatch(monkeypatch):
    """Bypass signature verification and capture whether an event was dispatched."""
    monkeypatch.setattr(github_webhooks, "verify_github_signature", lambda *a, **k: True)
    dispatched = []

    async def fake_dispatch(event, action, payload):
        dispatched.append((event, action, payload))

    monkeypatch.setattr(github_webhooks, "_dispatch_event", fake_dispatch)
    return dispatched


def _post(client, repo="acme/widgets"):
    return client.post(
        "/webhooks/github",
        json={"action": "opened", "repository": {"full_name": repo}},
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=stub",
            "X-GitHub-Delivery": "test-delivery",
        },
    )


@pytest.mark.asyncio
async def test_excluded_repo_is_skipped(client, monkeypatch, stub_signature_and_dispatch):
    monkeypatch.setattr(settings, "github_allowed_repos", "")
    monkeypatch.setattr(settings, "github_excluded_repos", "acme/widgets")

    resp = await _post(client, repo="acme/widgets")

    assert resp.json() == {"ok": True, "skipped": True}
    assert stub_signature_and_dispatch == []


@pytest.mark.asyncio
async def test_exclusion_wins_over_allowlist(client, monkeypatch, stub_signature_and_dispatch):
    # Repo is on both lists — exclusion must take precedence.
    monkeypatch.setattr(settings, "github_allowed_repos", "acme/widgets")
    monkeypatch.setattr(settings, "github_excluded_repos", "acme/widgets")

    resp = await _post(client, repo="acme/widgets")

    assert resp.json() == {"ok": True, "skipped": True}
    assert stub_signature_and_dispatch == []


@pytest.mark.asyncio
async def test_non_excluded_repo_is_dispatched(client, monkeypatch, stub_signature_and_dispatch):
    monkeypatch.setattr(settings, "github_allowed_repos", "")
    monkeypatch.setattr(settings, "github_excluded_repos", "acme/other")

    resp = await _post(client, repo="acme/widgets")

    assert resp.json() == {"ok": True}
    assert len(stub_signature_and_dispatch) == 1


@pytest.mark.asyncio
async def test_allowlist_still_filters(client, monkeypatch, stub_signature_and_dispatch):
    # No excludes, but repo isn't on the allowlist — existing behavior preserved.
    monkeypatch.setattr(settings, "github_allowed_repos", "acme/other")
    monkeypatch.setattr(settings, "github_excluded_repos", "")

    resp = await _post(client, repo="acme/widgets")

    assert resp.json() == {"ok": True, "skipped": True}
    assert stub_signature_and_dispatch == []
