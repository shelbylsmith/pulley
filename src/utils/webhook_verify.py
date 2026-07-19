import hashlib
import hmac
import time


def verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_slack_signature(
    payload: bytes, timestamp: str, signature: str, signing_secret: str
) -> bool:
    """Verify Slack request signature (v0)."""
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{payload.decode()}"
    digest = hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)
