from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from alice_office_router.channels import enabled_adapters
from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings, get_settings

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"
_API_TOKEN = "secret-api-token"
_API_MODULE = "alice_office_router.channels.api"
_LINE_ROOM = "line_U0123456789abcdef0123456789abcdef"
_MESSAGES_PATH = "/webhooks/api/messages"


def _settings(**overrides: object) -> Settings:
    """Build a Settings instance with test credentials, allowing overrides.

    Args:
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance suitable for unit tests.
    """
    defaults: dict[str, object] = {
        "LINE_CHANNEL_SECRET": TEST_SECRET,
        "LINE_CHANNEL_ACCESS_TOKEN": TEST_TOKEN,
        "HERMES_API_SERVER_KEY": "test_api_server_key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _build_app(settings: Settings) -> FastAPI:
    """Mount enabled_adapters(settings) on a fresh app, mirroring main.py.

    Args:
        settings: Settings whose API_CHANNEL_TOKEN drives whether the API
            channel is mounted at all (the gating under test).

    Returns:
        A FastAPI app with get_settings overridden to return these settings.
    """
    app = FastAPI()
    for adapter in enabled_adapters(settings):
        app.include_router(adapter.api_router(), prefix=f"/webhooks/{adapter.name}")

    def _override() -> Settings:
        return settings

    app.dependency_overrides[get_settings] = _override
    return app


@pytest.fixture
async def api_client() -> AsyncGenerator[AsyncClient, None]:
    """Provide a client bound to an app with the API channel enabled.

    Yields:
        AsyncClient for an app whose Settings carry a valid API_CHANNEL_TOKEN.
    """
    app = _build_app(_settings(API_CHANNEL_TOKEN=_API_TOKEN))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _auth_header(token: str = _API_TOKEN) -> dict[str, str]:
    """Build a Bearer Authorization header.

    Args:
        token: Bearer token to present.

    Returns:
        A headers dict carrying the Authorization header.
    """
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth — Bearer token (401)
# ---------------------------------------------------------------------------


async def test_missing_authorization_returns_401(api_client: AsyncClient) -> None:
    """A request without the Authorization header is rejected before core runs."""
    with patch(f"{_API_MODULE}.process_inbound", new=AsyncMock()) as mock_core:
        response = await api_client.post(
            _MESSAGES_PATH, json={"room_key": _LINE_ROOM, "text": "hi"}
        )

    assert response.status_code == 401
    mock_core.assert_not_awaited()


async def test_wrong_token_returns_401(api_client: AsyncClient) -> None:
    """A request with the wrong bearer token is rejected before core runs."""
    with patch(f"{_API_MODULE}.process_inbound", new=AsyncMock()) as mock_core:
        response = await api_client.post(
            _MESSAGES_PATH,
            headers=_auth_header("not-the-token"),
            json={"room_key": _LINE_ROOM, "text": "hi"},
        )

    assert response.status_code == 401
    mock_core.assert_not_awaited()


# ---------------------------------------------------------------------------
# Body validation (422)
# ---------------------------------------------------------------------------


async def test_bad_room_key_returns_422(api_client: AsyncClient) -> None:
    """A room_key that matches no accepted shape is a 422 (junk is rejected)."""
    response = await api_client.post(
        _MESSAGES_PATH,
        headers=_auth_header(),
        json={"room_key": "../etc/passwd", "text": "hi"},
    )

    assert response.status_code == 422


async def test_blank_text_returns_422(api_client: AsyncClient) -> None:
    """Whitespace-only text is a 422."""
    response = await api_client.post(
        _MESSAGES_PATH,
        headers=_auth_header(),
        json={"room_key": "api_dev", "text": "   "},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Happy path — passthrough to core.process_inbound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("room_key", [_LINE_ROOM, "api_dev"])
async def test_happy_path_passes_message_to_core(api_client: AsyncClient, room_key: str) -> None:
    """Both accepted room shapes reach core as an InboundMessage; replies pass through."""
    with patch(
        f"{_API_MODULE}.process_inbound", new=AsyncMock(return_value=["好", "第二則"])
    ) as mock_core:
        response = await api_client.post(
            _MESSAGES_PATH,
            headers=_auth_header(),
            json={"room_key": room_key, "text": "回覆一個字：好"},
        )

    assert response.status_code == 200
    assert response.json() == {"replies": ["好", "第二則"]}

    mock_core.assert_awaited_once()
    call = mock_core.await_args
    assert call is not None
    sent = call.args[0]
    assert isinstance(sent, InboundMessage)
    assert sent.channel == "api"
    assert sent.room_key == room_key
    assert sent.text == "回覆一個字：好"


# ---------------------------------------------------------------------------
# Gating — route absent when the token is unset
# ---------------------------------------------------------------------------


async def test_route_absent_when_token_unset() -> None:
    """With API_CHANNEL_TOKEN unset the channel is never mounted (route 404s)."""
    app = _build_app(_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            _MESSAGES_PATH,
            headers=_auth_header(),
            json={"room_key": "api_dev", "text": "hi"},
        )

    assert response.status_code == 404
