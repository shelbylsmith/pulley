import hashlib
import hmac
import time

from src.utils.webhook_verify import verify_github_signature, verify_slack_signature


def test_github_signature_valid():
    secret = "test-secret"
    payload = b'{"action": "opened"}'
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert verify_github_signature(payload, sig, secret) is True


def test_github_signature_invalid():
    assert verify_github_signature(b"payload", "sha256=bad", "secret") is False


def test_slack_signature_valid():
    signing_secret = "test-signing-secret"
    payload = b"token=xyz&command=/test"
    timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{payload.decode()}"
    sig = (
        "v0="
        + hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    )
    assert verify_slack_signature(payload, timestamp, sig, signing_secret) is True


def test_slack_signature_expired():
    signing_secret = "test"
    payload = b"data"
    old_timestamp = str(int(time.time()) - 600)
    assert verify_slack_signature(payload, old_timestamp, "v0=fake", signing_secret) is False
