from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from alice_office_router.config import Settings
from alice_office_router.google_oauth import (
    _exchange_code_for_token,
    _pending,
    account_key,
    auth_url_for,
    check_google_authorization,
    set_on_authorized,
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


def _write_tokens(
    settings: Settings, room_id: str, tokens: dict[str, object], member_key: str | None = None
) -> None:
    """Write one member's token file (see google_tokens module docstring).

    The gate reads a member file, not tokens.json directly — tokens.json is
    only ever the symlink core repoints per speaker. The member key defaults
    to the room's own account_key, which is what a 1:1 room's turns produce.
    """
    path = settings.room_google_member_tokens_path(room_id, member_key or account_key(room_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_module_state() -> None:
    """Keep the module-level pending map and authorized hook from leaking between tests."""
    _pending.clear()
    set_on_authorized(None)
    yield
    _pending.clear()
    set_on_authorized(None)


async def _settle_tasks() -> None:
    """Let the callback's detached on_authorized task run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)


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
        PUBLIC_BASE_URL="https://router.example.com",
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
    async def test_redirects_with_expected_query_params_and_stores_room_and_member(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, settings = app_client

        response = await client.get(
            "/oauth/start",
            params={"user_id": "U_ROOM_ABC", "member": "u_speaker"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in location
        assert "redirect_uri=https%3A%2F%2Frouter.example.com%2Foauth%2Fcallback" in location
        assert "response_type=code" in location
        assert "access_type=offline" in location
        assert "prompt=consent" in location

        # _pending must keep the original-case room_id (not the lowercased
        # account_key) so oauth_callback can locate the right per-room
        # directory later — see google_oauth module docstring — plus the
        # member the link was issued to, so the callback writes their file.
        assert len(_pending) == 1
        stored_room_id, stored_member, _created = next(iter(_pending.values()))
        assert (stored_room_id, stored_member) == ("U_ROOM_ABC", "u_speaker")

        # This room's own credential copy must have been seeded on demand,
        # since a brand-new room's directory doesn't exist before this.
        assert settings.room_google_web_creds_path("U_ROOM_ABC").exists()

    async def test_missing_user_id_returns_400(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, _ = app_client
        response = await client.get(
            "/oauth/start", params={"member": "u_speaker"}, follow_redirects=False
        )
        assert response.status_code == 400

    async def test_missing_member_returns_400(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, _ = app_client
        response = await client.get(
            "/oauth/start", params={"user_id": "U_ROOM_ABC"}, follow_redirects=False
        )
        assert response.status_code == 400
        assert _pending == {}

    @pytest.mark.parametrize(
        "member",
        ["U_SPEAKER", "../escape", "u speaker", "u" * 65, ""],
    )
    async def test_malformed_member_returns_400(
        self, app_client: tuple[AsyncClient, Settings], member: str
    ) -> None:
        """The member key becomes a filename, so only account_key's shape is accepted."""
        client, _ = app_client
        response = await client.get(
            "/oauth/start",
            params={"user_id": "U_ROOM_ABC", "member": member},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert _pending == {}

    async def test_disabled_returns_400(self, tmp_path: Path) -> None:
        from alice_office_router.config import get_settings
        from alice_office_router.main import app

        settings = _settings(tmp_path)  # no PUBLIC_BASE_URL, no web creds
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/oauth/start",
                    params={"user_id": "U1", "member": "u1"},
                    follow_redirects=False,
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /oauth/callback
# ---------------------------------------------------------------------------


async def _authorize(
    client: AsyncClient, *, member: str, scope: str = "https://www.googleapis.com/auth/calendar"
) -> httpx.Response:
    """Run /oauth/start then /oauth/callback for one member, with Google mocked out.

    Args:
        client: The ASGI test client.
        member: The member key the link is issued to.
        scope: The scope string Google's token response claims to have granted.

    Returns:
        The callback's HTTP response.
    """
    await client.get(
        "/oauth/start",
        params={"user_id": "U_ROOM_ABC", "member": member},
        follow_redirects=False,
    )
    state = next(iter(_pending))
    token_response = MagicMock()
    token_response.json.return_value = {
        "access_token": "access-123",
        "refresh_token": "refresh-123",
        "expires_in": 3600,
        "scope": scope,
    }
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=token_response)):
        return await client.get("/oauth/callback", params={"code": "auth-code", "state": state})


class TestOAuthCallback:
    async def test_happy_path_writes_the_member_file_keyed_by_the_room_account(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        """Which file is the member's; the key inside it is the room's (MCP env)."""
        client, settings = app_client

        response = await _authorize(client, member="u_speaker")

        assert response.status_code == 200
        assert "授權成功" in response.text

        tokens_path = settings.room_google_member_tokens_path("U_ROOM_ABC", "u_speaker")
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
        assert list(tokens) == ["u_room_abc"]
        stored = tokens["u_room_abc"]
        assert stored["access_token"] == "access-123"
        assert stored["refresh_token"] == "refresh-123"
        assert stored["token_type"] == "Bearer"
        assert stored["scope"] == "https://www.googleapis.com/auth/calendar"
        assert isinstance(stored["expiry_date"], int)
        # The other member's file is untouched: a group authorizes one by one.
        assert not settings.room_google_member_tokens_path("U_ROOM_ABC", "u_other").exists()

    async def test_on_authorized_hook_runs_after_the_token_is_on_disk(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        """Step 4 resumes the member's pending question here, so the token must already be readable."""
        client, settings = app_client
        seen: list[tuple[str, str, bool]] = []

        async def _hook(room_id: str, member_key: str) -> None:
            path = settings.room_google_member_tokens_path(room_id, member_key)
            seen.append((room_id, member_key, path.exists()))

        set_on_authorized(_hook)
        response = await _authorize(client, member="u_speaker")
        await _settle_tasks()

        assert response.status_code == 200
        assert seen == [("U_ROOM_ABC", "u_speaker", True)]

    async def test_a_raising_hook_is_logged_and_never_reaches_the_browser(
        self, app_client: tuple[AsyncClient, Settings], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, settings = app_client

        async def _hook(room_id: str, member_key: str) -> None:
            raise RuntimeError("resume blew up")

        set_on_authorized(_hook)
        with caplog.at_level(logging.ERROR, logger="alice_office_router.google_oauth"):
            response = await _authorize(client, member="u_speaker")
            await _settle_tasks()

        assert response.status_code == 200
        assert "授權成功" in response.text
        # The token still landed; only the follow-up failed.
        assert settings.room_google_member_tokens_path("U_ROOM_ABC", "u_speaker").exists()
        errors = [
            record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR
        ]
        assert len(errors) == 1
        assert "on_authorized hook failed" in errors[0]
        assert "resume blew up" in errors[0]

    async def test_no_hook_registered_is_a_no_op(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, _ = app_client
        response = await _authorize(client, member="u_speaker")
        await _settle_tasks()
        assert response.status_code == 200

    async def test_bad_state_returns_400(self, app_client: tuple[AsyncClient, Settings]) -> None:
        client, _ = app_client
        response = await client.get(
            "/oauth/callback", params={"code": "auth-code", "state": "nonexistent"}
        )
        assert response.status_code == 400

    async def test_no_access_token_in_response_returns_400(
        self, app_client: tuple[AsyncClient, Settings]
    ) -> None:
        client, _ = app_client
        await client.get(
            "/oauth/start",
            params={"user_id": "U_ROOM_ABC", "member": "u_speaker"},
            follow_redirects=False,
        )
        state = next(iter(_pending))

        token_response = MagicMock()
        token_response.json.return_value = {"error": "invalid_grant"}
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=token_response)):
            response = await client.get(
                "/oauth/callback", params={"code": "auth-code", "state": state}
            )

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# GOOGLE_TOKEN_URL (the exchange endpoint, redirectable for local e2e)
# ---------------------------------------------------------------------------


class TestTokenEndpointSetting:
    async def _post_call(self, settings: Settings) -> tuple[object, ...]:
        """Run one code exchange against a mocked httpx and return its call args.

        Args:
            settings: The settings whose GOOGLE_TOKEN_URL is under test.

        Returns:
            The positional arguments httpx.AsyncClient.post was called with.
        """
        token_response = MagicMock()
        token_response.json.return_value = {"access_token": "access-123"}
        post = AsyncMock(return_value=token_response)
        with patch.object(httpx.AsyncClient, "post", new=post):
            await _exchange_code_for_token("auth-code", settings, "client-id", "client-secret")
        return tuple(post.call_args.args)

    async def test_defaults_to_googles_own_token_endpoint(self, tmp_path: Path) -> None:
        """Any deployment that sets nothing still talks to Google."""
        settings = _settings(tmp_path)

        args = await self._post_call(settings)

        assert settings.GOOGLE_TOKEN_URL == "https://oauth2.googleapis.com/token"
        assert args[0] == "https://oauth2.googleapis.com/token"

    async def test_exchange_posts_to_the_configured_url(self, tmp_path: Path) -> None:
        """Local e2e points it at scripts/line_stub.py instead (docs/testing-paths.md)."""
        settings = _settings(tmp_path, GOOGLE_TOKEN_URL="http://localhost:8099/token")

        args = await self._post_call(settings)

        assert args[0] == "http://localhost:8099/token"


# ---------------------------------------------------------------------------
# auth_url_for
# ---------------------------------------------------------------------------


def test_auth_url_for_keeps_the_raw_room_id_and_names_the_member(tmp_path: Path) -> None:
    """The link carries the room in its original case plus whose token it will be."""
    settings = _settings(tmp_path, PUBLIC_BASE_URL="https://router.example.com")

    url = auth_url_for(settings, "U_ROOM_ABC", "u_speaker")

    assert url == "https://router.example.com/oauth/start?user_id=U_ROOM_ABC&member=u_speaker"


# ---------------------------------------------------------------------------
# check_google_authorization (the gate — informs, never blocks, since 2026-09-18)
# ---------------------------------------------------------------------------


_FULL_SCOPES = (
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/gmail.modify "
    "https://www.googleapis.com/auth/drive"
)
_NO_DRIVE_SCOPES = (
    "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.modify"
)


def _enabled_settings(tmp_path: Path) -> Settings:
    """Build Settings with the Google integration fully configured."""
    settings = _settings(tmp_path, PUBLIC_BASE_URL="https://router.example.com")
    _write_web_creds(settings)
    return settings


class TestCheckGoogleAuthorization:
    def test_disabled_returns_ok(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)  # no public URL / web creds => disabled
        assert check_google_authorization("U_ROOM_ABC", "u_room_abc", settings) == ("ok", None)

    def test_no_token_returns_unauthorized_without_blocking(self, tmp_path: Path) -> None:
        """Nothing is blocked and nothing is pushed; the turn just learns the speaker has none."""
        settings = _enabled_settings(tmp_path)

        assert check_google_authorization("U_ROOM_ABC", "u_room_abc", settings) == (
            "unauthorized",
            None,
        )

    def test_unidentified_group_speaker_returns_ok(self, tmp_path: Path) -> None:
        """A speaker LINE would not name has no token of their own, which is nothing to say."""
        settings = _enabled_settings(tmp_path)

        assert check_google_authorization("C_GROUP", None, settings) == ("ok", None)

    def test_token_missing_drive_scope_returns_notice_naming_the_member(
        self, tmp_path: Path
    ) -> None:
        settings = _enabled_settings(tmp_path)
        _write_tokens(
            settings,
            "U_ROOM_ABC",
            {
                "u_room_abc": {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expiry_date": int(_now_ms() + 3_600_000),
                    "scope": _NO_DRIVE_SCOPES,
                }
            },
            member_key="u_speaker",
        )

        status, message = check_google_authorization("U_ROOM_ABC", "u_speaker", settings)

        assert status == "notice"
        assert message is not None
        assert "/oauth/start?user_id=U_ROOM_ABC&member=u_speaker" in message

    def test_full_scopes_valid_expiry_returns_ok(self, tmp_path: Path) -> None:
        settings = _enabled_settings(tmp_path)
        _write_tokens(
            settings,
            "U_ROOM_ABC",
            {
                "u_room_abc": {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expiry_date": int(_now_ms() + 3_600_000),
                    "scope": _FULL_SCOPES,
                }
            },
        )

        assert check_google_authorization("U_ROOM_ABC", "u_room_abc", settings) == ("ok", None)

    def test_expired_without_refresh_token_returns_unauthorized(self, tmp_path: Path) -> None:
        """An unusable token is the same case as no token: warn the agent, let the turn run."""
        settings = _enabled_settings(tmp_path)
        _write_tokens(
            settings,
            "U_ROOM_ABC",
            {
                "u_room_abc": {
                    "access_token": "a",
                    "refresh_token": "",
                    "expiry_date": int(_now_ms() - 3_600_000),
                    "scope": _FULL_SCOPES,
                }
            },
        )

        assert check_google_authorization("U_ROOM_ABC", "u_room_abc", settings) == (
            "unauthorized",
            None,
        )

    def test_expired_with_refresh_token_and_full_scopes_returns_ok(self, tmp_path: Path) -> None:
        settings = _enabled_settings(tmp_path)
        _write_tokens(
            settings,
            "U_ROOM_ABC",
            {
                "u_room_abc": {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expiry_date": int(_now_ms() - 3_600_000),
                    "scope": _FULL_SCOPES,
                }
            },
        )

        assert check_google_authorization("U_ROOM_ABC", "u_room_abc", settings) == ("ok", None)

    def test_malformed_tokens_json_returns_unauthorized(self, tmp_path: Path) -> None:
        """An unreadable token file reads as no token, which the agent is told about."""
        settings = _enabled_settings(tmp_path)
        tokens_path = settings.room_google_member_tokens_path("U_ROOM_ABC", "u_room_abc")
        tokens_path.parent.mkdir(parents=True, exist_ok=True)
        tokens_path.write_text("not valid json {{{", encoding="utf-8")

        assert check_google_authorization("U_ROOM_ABC", "u_room_abc", settings) == (
            "unauthorized",
            None,
        )


def _now_ms() -> float:
    """Return the current time in milliseconds, matching tokens.json's expiry_date unit."""
    import time

    return time.time() * 1000
