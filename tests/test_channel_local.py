from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from alice_office_router.channels.base import InboundMessage
from alice_office_router.channels.local import CollectingResponder
from alice_office_router.config import Settings

TEST_LOCAL_TOKEN = "test-local-token"

_ENDPOINT = "/channels/local/messages"
_AUTH = {"Authorization": f"Bearer {TEST_LOCAL_TOKEN}"}


def _settings(**overrides: object) -> Settings:
    """Build a Settings instance with the local channel enabled.

    Args:
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance suitable for unit tests.
    """
    defaults: dict[str, object] = {
        "LINE_CHANNEL_SECRET": "test_channel_secret",
        "LINE_CHANNEL_ACCESS_TOKEN": "test_channel_access_token",
        "HERMES_API_SERVER_KEY": "test_api_server_key",
        "LOCAL_CHANNEL_TOKEN": TEST_LOCAL_TOKEN,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _override_settings(**overrides: object) -> None:
    """Install a settings dependency override on the app under test.

    Args:
        **overrides: Field overrides forwarded to `_settings`.
    """
    from alice_office_router.config import get_settings
    from alice_office_router.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(**overrides)


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    """Reset app dependency overrides after each test."""
    from alice_office_router.main import app

    yield  # type: ignore[misc]
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_disabled_channel_returns_403(client: AsyncClient) -> None:
    """With LOCAL_CHANNEL_TOKEN unset the endpoint must refuse everything."""
    _override_settings(LOCAL_CHANNEL_TOKEN="")

    response = await client.post(
        _ENDPOINT, json={"room_id": "local_dev", "text": "hi"}, headers=_AUTH
    )
    assert response.status_code == 403


async def test_missing_token_returns_401(client: AsyncClient) -> None:
    _override_settings()

    response = await client.post(_ENDPOINT, json={"room_id": "local_dev", "text": "hi"})
    assert response.status_code == 401


async def test_wrong_token_returns_401(client: AsyncClient) -> None:
    _override_settings()

    response = await client.post(
        _ENDPOINT,
        json={"room_id": "local_dev", "text": "hi"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_room_id", ["../etc", "a/b", "", "-leading", "room id", "a" * 65])
async def test_unsafe_room_id_returns_422(client: AsyncClient, bad_room_id: str) -> None:
    """Room ids unusable as container names / path segments are rejected upfront."""
    _override_settings()

    response = await client.post(
        _ENDPOINT, json={"room_id": bad_room_id, "text": "hi"}, headers=_AUTH
    )
    assert response.status_code == 422


async def test_empty_text_returns_422(client: AsyncClient) -> None:
    _override_settings()

    response = await client.post(
        _ENDPOINT, json={"room_id": "local_dev", "text": ""}, headers=_AUTH
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_returns_pipeline_deliveries_in_order(client: AsyncClient) -> None:
    """The response carries the pipeline outcome plus every delivered text."""
    _override_settings()

    async def fake_pipeline(
        message: InboundMessage, responder: CollectingResponder, config: Settings
    ) -> str:
        assert message == InboundMessage(channel="local", room_id="local_dev", text="哈囉")
        await responder.send_notice("缺少 Drive 授權：https://example.com/oauth/start")
        await responder.send_reply("哈囉，我是 Hermes")
        return "replied"

    with patch(
        "alice_office_router.channels.local.process_inbound",
        new=AsyncMock(side_effect=fake_pipeline),
    ):
        response = await client.post(
            _ENDPOINT, json={"room_id": "local_dev", "text": "哈囉"}, headers=_AUTH
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "replied",
        "messages": ["缺少 Drive 授權：https://example.com/oauth/start", "哈囉，我是 Hermes"],
    }


async def test_pipeline_failure_surfaces_status_with_empty_messages(client: AsyncClient) -> None:
    """A pipeline failure yields 200 with the error status — details go to the log."""
    _override_settings()

    with patch(
        "alice_office_router.channels.local.process_inbound",
        new=AsyncMock(return_value="agent_error"),
    ):
        response = await client.post(
            _ENDPOINT, json={"room_id": "local_dev", "text": "哈囉"}, headers=_AUTH
        )

    assert response.status_code == 200
    assert response.json() == {"status": "agent_error", "messages": []}
