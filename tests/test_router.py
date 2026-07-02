from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"


@pytest.fixture(autouse=True)
def patch_settings() -> None:
    """Patch get_settings to return test credentials for all router tests."""
    from alice_office_router.config import Settings

    mock_settings = Settings(
        LINE_CHANNEL_SECRET=TEST_SECRET,
        LINE_CHANNEL_ACCESS_TOKEN=TEST_TOKEN,
        HERMES_API_SERVER_KEY="test_api_server_key",
    )
    with patch("alice_office_router.router.get_settings", return_value=mock_settings):
        pass


@pytest.fixture(autouse=True)
def override_settings_dep() -> None:
    """Override the settings dependency on the FastAPI app for test isolation."""
    from alice_office_router.config import Settings, get_settings
    from alice_office_router.main import app

    def _test_settings() -> Settings:
        return Settings(
            LINE_CHANNEL_SECRET=TEST_SECRET,
            LINE_CHANNEL_ACCESS_TOKEN=TEST_TOKEN,
            HERMES_API_SERVER_KEY="test_api_server_key",
        )

    app.dependency_overrides[get_settings] = _test_settings
    yield  # type: ignore[misc]
    app.dependency_overrides.clear()


async def test_missing_signature_returns_400(client: AsyncClient, line_webhook_body: bytes) -> None:
    """POST /webhook without x-line-signature header should return 400."""
    response = await client.post(
        "/webhook",
        content=line_webhook_body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


async def test_invalid_signature_returns_400(client: AsyncClient, line_webhook_body: bytes) -> None:
    """POST /webhook with a wrong signature should return 400."""
    response = await client.post(
        "/webhook",
        content=line_webhook_body,
        headers={
            "Content-Type": "application/json",
            "x-line-signature": "invalidsignature==",
        },
    )
    assert response.status_code == 400


async def test_valid_request_returns_200_ok(
    client: AsyncClient,
    line_webhook_body: bytes,
    valid_signature: str,
) -> None:
    """POST /webhook with a valid signature and body should return 200 {"status": "ok"}."""
    with (
        patch(
            "alice_office_router.router.get_or_create_container",
            return_value="http://hermes_room_TEST123:8642",
        ),
        patch(
            "alice_office_router.router._process_and_reply",
            new_callable=AsyncMock,
        ),
    ):
        response = await client.post(
            "/webhook",
            content=line_webhook_body,
            headers={
                "Content-Type": "application/json",
                "x-line-signature": valid_signature,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_empty_events_returns_200_ok(
    client: AsyncClient,
    valid_signature: str,
) -> None:
    """POST /webhook with empty events list should return 200 without dispatching."""
    empty_body = json.dumps({"events": []}).encode("utf-8")

    import base64
    import hashlib
    import hmac as hmac_mod

    digest = hmac_mod.new(TEST_SECRET.encode(), empty_body, hashlib.sha256).digest()
    sig = base64.b64encode(digest).decode()

    response = await client.post(
        "/webhook",
        content=empty_body,
        headers={
            "Content-Type": "application/json",
            "x-line-signature": sig,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_missing_room_id_returns_400(
    client: AsyncClient,
) -> None:
    """POST /webhook where source has no roomId/userId/groupId should return 400."""
    import base64
    import hashlib
    import hmac as hmac_mod

    bad_body = json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "source": {"type": "room"},  # roomId missing
                    "message": {"type": "text", "text": "hi"},
                }
            ]
        }
    ).encode("utf-8")
    digest = hmac_mod.new(TEST_SECRET.encode(), bad_body, hashlib.sha256).digest()
    sig = base64.b64encode(digest).decode()

    response = await client.post(
        "/webhook",
        content=bad_body,
        headers={"Content-Type": "application/json", "x-line-signature": sig},
    )
    assert response.status_code == 400


async def test_process_and_reply_pushes_agent_response_to_line() -> None:
    """_process_and_reply resolves the container, asks the agent, and pushes the reply."""
    from alice_office_router.config import Settings
    from alice_office_router.router import _process_and_reply

    settings = Settings(
        LINE_CHANNEL_SECRET=TEST_SECRET,
        LINE_CHANNEL_ACCESS_TOKEN=TEST_TOKEN,
        HERMES_API_SERVER_KEY="test_api_server_key",
    )

    with (
        patch(
            "alice_office_router.router.get_or_create_container",
            return_value="http://hermes_room_AAA:8642",
        ) as mock_get_container,
        patch(
            "alice_office_router.router.ask_hermes_agent",
            new=AsyncMock(return_value="哈囉，我是 Hermes"),
        ) as mock_ask,
        patch(
            "alice_office_router.router.push_line_message", new=AsyncMock()
        ) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings)

    mock_get_container.assert_called_once_with("room_AAA", settings)
    mock_ask.assert_awaited_once_with(
        "http://hermes_room_AAA:8642", "room_AAA", "哈囉", "test_api_server_key"
    )
    mock_push.assert_awaited_once_with("room_AAA", "哈囉，我是 Hermes", TEST_TOKEN)


async def test_process_and_reply_logs_and_returns_on_agent_error() -> None:
    """A Hermes agent failure is logged; it must not raise or push anything to LINE."""
    from alice_office_router.config import Settings
    from alice_office_router.router import _process_and_reply

    settings = Settings(
        LINE_CHANNEL_SECRET=TEST_SECRET,
        LINE_CHANNEL_ACCESS_TOKEN=TEST_TOKEN,
        HERMES_API_SERVER_KEY="test_api_server_key",
    )

    with (
        patch(
            "alice_office_router.router.get_or_create_container",
            return_value="http://hermes_room_AAA:8642",
        ),
        patch(
            "alice_office_router.router.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
        patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings)

    mock_push.assert_not_called()
