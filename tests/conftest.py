from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from alice_office_router.config import Settings

# Unit tests must be hermetic: never read the developer's real .env, whose
# optional keys (HOST_SECRETARY_MCP_DIR, GOOGLE_MAPS_API_KEY, …) would leak
# into Settings() instances constructed by tests. Must run before test modules
# import — module-level Settings (e.g. SETTINGS_IN_DOCKER) are built at import time.
Settings.model_config["env_file"] = None

from alice_office_router.main import app  # noqa: E402

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"


def _compute_signature(body: bytes, secret: str) -> str:
    """Compute the LINE HMAC-SHA256 signature for a given body and secret.

    Args:
        body: Raw bytes to sign.
        secret: HMAC key (LINE channel secret).

    Returns:
        Base64-encoded HMAC-SHA256 digest string.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP client bound to the FastAPI app under test.

    Yields:
        AsyncClient configured with ASGI transport for the app.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def line_webhook_body() -> bytes:
    """Return a valid LINE webhook JSON body as bytes.

    Returns:
        JSON-encoded webhook event bytes.
    """
    payload = {
        "events": [
            {
                "type": "message",
                "source": {"type": "room", "roomId": "room_TEST123"},
                "message": {"type": "text", "text": "Hello"},
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def valid_signature(line_webhook_body: bytes) -> str:
    """Compute a valid LINE signature for the test webhook body.

    Args:
        line_webhook_body: Raw webhook body bytes fixture.

    Returns:
        Base64-encoded HMAC-SHA256 signature string.
    """
    return _compute_signature(line_webhook_body, TEST_SECRET)
