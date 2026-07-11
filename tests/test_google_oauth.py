from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from alice_office_router.config import Settings
from alice_office_router.google_oauth import (
    _pending,
    account_key,
    check_google_authorization,
)

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Build a Settings instance rooted at tmp_path, allowing overrides.

    Args:
        tmp_path: Pytest tmp_path fixture, used as DATA_DIR/HOST_DATA_DIR.
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance suitable for unit tests.
    """
    defaults: dict[str, object] = {
        "LINE_CHANNEL_SECRET": TEST_SECRET,
        "LINE_CHANNEL_ACCESS_TOKEN": TEST_TOKEN,
        "HERMES_API_SERVER_KEY": "test_api_server_key",
        "DATA_DIR": tmp_path,
        "HOST_DATA_DIR": tmp_path,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _write_web_creds(settings: Settings) -> None:
    """Write a fake Web application GCP OAuth client JSON under settings.google_web_creds_path."""
    settings.google_web_creds_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_web_creds_path.write_text(
        json.dumps({"web": {"client_id": "test-client-id", "client_secret": "test-client-secret"}}),
        encoding="utf-8",
    )


def _write_tokens(settings: Settings, tokens: dict[str, object]) -> None:
    """Write tokens.json content under settings.google_tokens_path."""
    settings.google_tokens_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_tokens_path.write_text(json.dumps(tokens), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_pending() -> None:
    """Ensure the module-level pending-state dict doesn't leak between tests."""
    _pending.clear()
    yield
    _pending.clear()


@pytest.fixture
async def app_client(tmp_path: Path):
    """Build an ASGI test client with get_settings overridden to a tmp-rooted Settings.

    Args:
        tmp_path: Pytest tmp_path fixture.

    Yields:
        Tuple of (AsyncClient, Settings) for use in route-level tests.
    """
    from alice_office_router.config import get_settings
    from alice_office_router.main import app

    settings = _settings(
        tmp_path,
        GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com",
    )
    _write_web_creds(settings)

    def _override() -> Settings:
        return settings

    app.dependency_overrides[get_settings] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, settings
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# account_key
# ---------------------------------------------------------------------------


def test_account_key_lowercases_room_id() -> None:
    """LINE room ids (U/C/R-prefixed) must be lowercased for the Google account key."""
    assert account_key("U196D1445F7FE156EAC44C02106F364EC") == "u196d1445f7fe156eac44c02106f364ec"


# ---------------------------------------------------------------------------
# GET /oauth/start
# ---------------------------------------------------------------------------


class TestOAuthStart:
    async def test_redirects_with_expected_query_params_and_lowercased_key(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, settings = app_client

        response = await client.get(
            "/oauth/start", params={"user_id": "U_ROOM_ABC"}, follow_redirects=False
        )

        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in location
        assert "redirect_uri=https%3A%2F%2Frouter.example.com%2Foauth%2Fcallback" in location
        assert "response_type=code" in location
        assert "access_type=offline" in location
        assert "prompt=consent" in location

        assert len(_pending) == 1
        stored_key, _ = next(iter(_pending.values()))
        assert stored_key == "u_room_abc"

    async def test_missing_user_id_returns_400(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, _ = app_client
        response = await client.get("/oauth/start", follow_redirects=False)
        assert response.status_code == 400

    async def test_disabled_returns_400(self, tmp_path: Path) -> None:
        from alice_office_router.config import get_settings
        from alice_office_router.main import app

        settings = _settings(tmp_path)  # no GOOGLE_OAUTH_PUBLIC_URL, no web creds
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/oauth/start", params={"user_id": "U1"}, follow_redirects=False
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /oauth/callback
# ---------------------------------------------------------------------------


class TestOAuthCallback:
    async def test_happy_path_writes_tokens_keyed_by_lowercase_account(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, settings = app_client
        start_resp = await client.get(
            "/oauth/start", params={"user_id": "U_ROOM_ABC"}, follow_redirects=False
        )
        state = next(iter(_pending))
        assert start_resp.status_code == 302

        token_response = MagicMock()
        token_response.json.return_value = {
            "access_token": "access-123",
            "refresh_token": "refresh-123",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/calendar",
        }
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=token_response)):
            response = await client.get(
                "/oauth/callback", params={"code": "auth-code", "state": state}
            )

        assert response.status_code == 200
        assert "授權成功" in response.text

        tokens = json.loads(settings.google_tokens_path.read_text(encoding="utf-8"))
        assert "u_room_abc" in tokens
        stored = tokens["u_room_abc"]
        assert stored["access_token"] == "access-123"
        assert stored["refresh_token"] == "refresh-123"
        assert stored["token_type"] == "Bearer"
        assert stored["scope"] == "https://www.googleapis.com/auth/calendar"
        assert isinstance(stored["expiry_date"], int)

    async def test_bad_state_returns_400(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, _ = app_client
        response = await client.get(
            "/oauth/callback", params={"code": "auth-code", "state": "nonexistent"}
        )
        assert response.status_code == 400

    async def test_no_access_token_in_response_returns_400(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, _ = app_client
        await client.get("/oauth/start", params={"user_id": "U_ROOM_ABC"}, follow_redirects=False)
        state = next(iter(_pending))

        token_response = MagicMock()
        token_response.json.return_value = {"error": "invalid_grant"}
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=token_response)):
            response = await client.get(
                "/oauth/callback", params={"code": "auth-code", "state": state}
            )

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# check_google_authorization (the gate)
# ---------------------------------------------------------------------------


class TestCheckGoogleAuthorization:
    def test_disabled_returns_ok(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)  # no public URL / web creds => disabled
        status, message = check_google_authorization("U_ROOM_ABC", settings)
        assert (status, message) == ("ok", None)

    def test_gate_flag_false_returns_ok_even_if_configured(self, tmp_path: Path) -> None:
        settings = _settings(
            tmp_path, GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com", GOOGLE_OAUTH_GATE=False
        )
        _write_web_creds(settings)
        status, message = check_google_authorization("U_ROOM_ABC", settings)
        assert (status, message) == ("ok", None)

    def test_no_token_returns_blocked_with_lowercased_auth_link(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com")
        _write_web_creds(settings)

        status, message = check_google_authorization("U_ROOM_ABC", settings)

        assert status == "blocked"
        assert message is not None
        assert "/oauth/start?user_id=u_room_abc" in message

    def test_token_missing_drive_scope_returns_notice_and_allows(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com")
        _write_web_creds(settings)
        far_future_ms = int(_now_ms() + 3_600_000)
        _write_tokens(
            settings,
            {
                "u_room_abc": {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expiry_date": far_future_ms,
                    "scope": (
                        "https://www.googleapis.com/auth/calendar "
                        "https://www.googleapis.com/auth/gmail.modify"
                    ),
                }
            },
        )

        status, message = check_google_authorization("U_ROOM_ABC", settings)

        assert status == "notice"
        assert message is not None
        assert "/oauth/start?user_id=u_room_abc" in message

    def test_full_scopes_valid_expiry_returns_ok(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com")
        _write_web_creds(settings)
        far_future_ms = int(_now_ms() + 3_600_000)
        _write_tokens(
            settings,
            {
                "u_room_abc": {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expiry_date": far_future_ms,
                    "scope": (
                        "https://www.googleapis.com/auth/calendar "
                        "https://www.googleapis.com/auth/gmail.modify "
                        "https://www.googleapis.com/auth/drive"
                    ),
                }
            },
        )

        status, message = check_google_authorization("U_ROOM_ABC", settings)
        assert (status, message) == ("ok", None)

    def test_expired_without_refresh_token_returns_blocked(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com")
        _write_web_creds(settings)
        past_ms = int(_now_ms() - 3_600_000)
        _write_tokens(
            settings,
            {
                "u_room_abc": {
                    "access_token": "a",
                    "refresh_token": "",
                    "expiry_date": past_ms,
                    "scope": (
                        "https://www.googleapis.com/auth/calendar "
                        "https://www.googleapis.com/auth/gmail.modify "
                        "https://www.googleapis.com/auth/drive"
                    ),
                }
            },
        )

        status, _ = check_google_authorization("U_ROOM_ABC", settings)
        assert status == "blocked"

    def test_expired_with_refresh_token_and_full_scopes_returns_ok(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com")
        _write_web_creds(settings)
        past_ms = int(_now_ms() - 3_600_000)
        _write_tokens(
            settings,
            {
                "u_room_abc": {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expiry_date": past_ms,
                    "scope": (
                        "https://www.googleapis.com/auth/calendar "
                        "https://www.googleapis.com/auth/gmail.modify "
                        "https://www.googleapis.com/auth/drive"
                    ),
                }
            },
        )

        status, message = check_google_authorization("U_ROOM_ABC", settings)
        assert (status, message) == ("ok", None)

    def test_malformed_tokens_json_returns_blocked(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com")
        _write_web_creds(settings)
        settings.google_tokens_path.parent.mkdir(parents=True, exist_ok=True)
        settings.google_tokens_path.write_text("not valid json {{{", encoding="utf-8")

        status, _ = check_google_authorization("U_ROOM_ABC", settings)
        assert status == "blocked"


def _now_ms() -> float:
    """Return the current time in milliseconds, matching tokens.json's expiry_date unit."""
    import time

    return time.time() * 1000
