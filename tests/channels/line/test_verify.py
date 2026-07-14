from __future__ import annotations

import base64
import hashlib
import hmac

from alice_office_router.channels.line.verify import verify_line_signature

SECRET = "my_test_secret"
BODY = b'{"events":[{"type":"message"}]}'


def _make_sig(body: bytes, secret: str) -> str:
    """Compute expected HMAC-SHA256 signature for test assertions.

    Args:
        body: Raw bytes to sign.
        secret: HMAC key.

    Returns:
        Base64-encoded signature string.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def test_valid_signature_returns_true() -> None:
    """A correctly computed signature should return True."""
    sig = _make_sig(BODY, SECRET)
    assert verify_line_signature(BODY, sig, SECRET) is True


def test_invalid_signature_returns_false() -> None:
    """A tampered signature should return False."""
    assert verify_line_signature(BODY, "invalidsignature==", SECRET) is False


def test_empty_signature_returns_false() -> None:
    """An empty signature string should return False without error."""
    assert verify_line_signature(BODY, "", SECRET) is False


def test_wrong_secret_returns_false() -> None:
    """A signature computed with a different secret should return False."""
    sig = _make_sig(BODY, "wrong_secret")
    assert verify_line_signature(BODY, sig, SECRET) is False


def test_empty_body_valid_signature() -> None:
    """An empty body with a matching signature should return True."""
    empty_body = b""
    sig = _make_sig(empty_body, SECRET)
    assert verify_line_signature(empty_body, sig, SECRET) is True
