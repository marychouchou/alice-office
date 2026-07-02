from __future__ import annotations

import base64
import hashlib
import hmac


def verify_line_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify a LINE webhook HMAC-SHA256 signature.

    Computes HMAC-SHA256 over the raw request body using the channel secret,
    then base64-encodes the digest and compares it with the provided signature
    using a constant-time comparison to prevent timing attacks.

    Args:
        body: Raw request body bytes.
        signature: Base64-encoded HMAC-SHA256 signature from the x-line-signature header.
        secret: LINE channel secret used as the HMAC key.

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not signature:
        return False

    secret_bytes = secret.encode("utf-8")
    digest = hmac.new(secret_bytes, body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)
