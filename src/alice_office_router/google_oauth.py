"""Google OAuth authorization for LINE rooms.

Reimplements the semantics of the standalone google-workspace-pack's
oauth-server/oauth_server.py (Flask) and plugins/oauth_gate/__init__.py
(Hermes plugin) as FastAPI routes + a plain function this router calls
directly. Both must be reimplemented here rather than ported as-is:

- The oauth-server was a separate Flask process; this router is already the
  public HTTPS endpoint, so its routes become part of this app instead.
- oauth_gate cannot work as a Hermes plugin in this deployment: router->agent
  traffic uses Hermes's api_server platform (/v1/chat/completions), which
  bypasses the pre_gateway_dispatch hook the pack's plugin relied on. The
  gate check must run in the router itself, before a message ever reaches
  the agent.

tokens.json and both GCP credential files live per room, under
Settings.room_google_dir(room_id) — never a shared/global location (see
container_manager.ensure_google_seed). Two different identifiers are both in
play and must not be conflated:

- room_id: the raw LINE room/user/group id (starts with uppercase U/C/R),
  used as-is for every filesystem path (DATA_DIR/room_id/google/...) — must
  keep its original case, or it silently diverges from the directory
  container_manager creates for the room's data/mcp/plugins.
- account_key(room_id): the same id lowercased, used only as the dict key
  *inside* a room's tokens.json and for GOOGLE_ACCOUNT_MODE, because
  @cocal/google-calendar-mcp validates that env var against
  /^[a-z0-9_-]{1,64}$/ (lowercase only).

_pending stores the raw room_id (not the account_key) precisely so
oauth_callback can recover the correct on-disk directory.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from alice_office_router.config import Settings, get_settings
from alice_office_router.container_manager import ensure_google_seed

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scopes requested during the interactive OAuth consent flow.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]

# Scopes the gate requires before it will stop nagging the user; a subset of
# SCOPES (calendar.events is implied by calendar for gate purposes).
REQUIRED_SCOPES = {
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
}

# How long a state token started via /oauth/start stays valid, in seconds.
_PENDING_TTL_SECONDS = 600.0

# Process-local map of state -> (room_id, created_ts) — room_id kept in its
# original case (see module docstring). Mirrors the pack's in-memory PENDING
# dict: a router restart invalidates any in-flight authorization, and this
# is not shared across multiple router workers/processes (fine for the
# current single-worker deployment).
_pending: dict[str, tuple[str, float]] = {}

_BLOCKED_MSG_TEMPLATE = (
    "🔐 請先授權 Google 帳號，才能操作 Calendar、Gmail 和 Drive。\n\n"
    "👉 點此授權：{auth_url}\n\n"
    "授權完成後，請再傳一次您的問題！"
)
_NOTICE_MSG_TEMPLATE = (
    "🔄 需要重新授權以啟用 Google Drive 功能。\n\n"
    "👉 點此重新授權（包含 Drive）：{auth_url}\n\n"
    "授權完成後即可使用 Drive 功能！"
)

_SUCCESS_HTML = """
    <html><body>
    <h2>授權成功，請回到 LINE 繼續使用。</h2>
    </body></html>
"""

oauth_router = APIRouter()


def account_key(room_id: str) -> str:
    """Normalize a LINE room id into the Google account key used everywhere.

    @cocal/google-calendar-mcp validates GOOGLE_ACCOUNT_MODE against
    /^[a-z0-9_-]{1,64}$/, which rejects LINE's uppercase-prefixed room ids
    (U.../C.../R...) outright. Lowercasing is therefore mandatory, not
    cosmetic — tokens.json keys, oauth routes, and the gate must all agree.

    Args:
        room_id: Raw LINE room/user/group id.

    Returns:
        The lowercased room id, used as the Google account key.
    """
    return room_id.lower()


def _purge_expired_pending() -> None:
    """Drop pending OAuth states older than _PENDING_TTL_SECONDS.

    Called on every access to _pending so it never grows unbounded across a
    long-lived process, without needing a background task.
    """
    now = time.monotonic()
    expired = [
        state for state, (_, created) in _pending.items() if now - created > _PENDING_TTL_SECONDS
    ]
    for state in expired:
        del _pending[state]


def _load_tokens(config: Settings, room_id: str) -> dict[str, dict[str, object]]:
    """Load one room's own tokens.json, tolerating a missing file.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).

    Returns:
        Mapping of account_key to that account's token data, or an empty
        dict if the file does not exist yet. In practice this room's
        tokens.json holds at most one entry, since each room's directory is
        no longer shared with any other room.
    """
    path = config.room_google_tokens_path(room_id)
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    tokens: dict[str, dict[str, object]] = json.loads(raw)
    return tokens


def _save_tokens(config: Settings, room_id: str, tokens: dict[str, dict[str, object]]) -> None:
    """Write one room's own tokens.json, creating parent directories as needed.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).
        tokens: Full account_key -> token data mapping to persist.
    """
    path = config.room_google_tokens_path(room_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def _load_web_credentials(config: Settings, room_id: str) -> tuple[str, str]:
    """Load one room's own Web application OAuth client id/secret.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).

    Returns:
        Tuple of (client_id, client_secret).

    Raises:
        HTTPException: 400 if the credentials file is missing or malformed.
    """
    try:
        raw = config.room_google_web_creds_path(room_id).read_text(encoding="utf-8")
        data = json.loads(raw)
        web = data["web"]
        return str(web["client_id"]), str(web["client_secret"])
    except (OSError, KeyError, ValueError) as exc:
        logger.error(f"Failed to load Google web credentials for room [{room_id}]: {exc}")
        raise HTTPException(status_code=400, detail="Google OAuth not configured") from exc


@oauth_router.get("/oauth/start")
async def oauth_start(
    config: Annotated[Settings, Depends(get_settings)],
    user_id: str | None = None,
) -> RedirectResponse:
    """Start the Google OAuth consent flow for a LINE room.

    Args:
        user_id: The raw LINE room/user/group id (query param), remembered
            as-is (original case) against the state — see module docstring
            for why this must not be lowercased here.
        config: Application settings via dependency injection.

    Returns:
        A 302 redirect to Google's OAuth consent screen.

    Raises:
        HTTPException: 400 if user_id is missing or Google OAuth isn't configured.
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id")
    if not config.google_oauth_enabled:
        raise HTTPException(status_code=400, detail="Google OAuth not configured")

    # This may be this room's very first contact with the filesystem: the
    # gate blocks a new room before get_or_create_container ever runs (see
    # router._apply_google_gate), so data/<room_id>/google/ might not exist
    # yet. ensure_google_seed is idempotent — a no-op if already seeded.
    ensure_google_seed(user_id, config)
    client_id, _ = _load_web_credentials(config, user_id)

    _purge_expired_pending()
    state = secrets.token_urlsafe(16)
    _pending[state] = (user_id, time.monotonic())

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": f"{config.GOOGLE_OAUTH_PUBLIC_URL}/oauth/callback",
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return RedirectResponse(url=f"{GOOGLE_AUTH_URL}?{query}", status_code=302)


async def _exchange_code_for_token(
    code: str, config: Settings, client_id: str, client_secret: str
) -> dict[str, object]:
    """Exchange an OAuth authorization code for a token response.

    Args:
        code: Authorization code returned by Google.
        config: Application settings (for the redirect_uri).
        client_id: Web application OAuth client id.
        client_secret: Web application OAuth client secret.

    Returns:
        Google's token endpoint JSON response.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": f"{config.GOOGLE_OAUTH_PUBLIC_URL}/oauth/callback",
                "grant_type": "authorization_code",
            },
        )
        result: dict[str, object] = response.json()
        return result


@oauth_router.get("/oauth/callback")
async def oauth_callback(
    config: Annotated[Settings, Depends(get_settings)],
    code: str | None = None,
    state: str | None = None,
) -> HTMLResponse:
    """Handle Google's OAuth redirect, exchanging the code and storing the token.

    Args:
        code: Authorization code query param from Google.
        state: Opaque state query param, matched against a pending /oauth/start call.
        config: Application settings via dependency injection.

    Returns:
        A small zh-TW success HTML page.

    Raises:
        HTTPException: 400 on invalid/expired state, or if Google's token
            response has no access_token.
    """
    _purge_expired_pending()
    pending_entry = _pending.pop(state, None) if state else None
    if pending_entry is None:
        raise HTTPException(status_code=400, detail="Invalid or expired state")
    room_id, _ = pending_entry
    key = account_key(room_id)

    client_id, client_secret = _load_web_credentials(config, room_id)
    token_response = await _exchange_code_for_token(code or "", config, client_id, client_secret)

    if "access_token" not in token_response:
        logger.error(f"Google OAuth token exchange failed for {key}: {token_response}")
        raise HTTPException(status_code=400, detail="OAuth failed")

    _store_token(config, room_id, key, token_response)
    return HTMLResponse(content=_SUCCESS_HTML)


def _store_token(
    config: Settings, room_id: str, key: str, token_response: dict[str, object]
) -> None:
    """Merge a freshly exchanged token into this room's own tokens.json.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case), used to
            locate this room's own tokens.json.
        key: account_key to store the token under, inside that file.
        token_response: Google's token endpoint JSON response.
    """
    tokens = _load_tokens(config, room_id)
    expires_in = token_response.get("expires_in", 0)
    now_ms = int(time.time() * 1000)
    tokens[key] = {
        "access_token": token_response["access_token"],
        "refresh_token": token_response.get("refresh_token", ""),
        "expiry_date": now_ms + int(expires_in) * 1000,  # type: ignore[call-overload]
        "token_type": "Bearer",
        "scope": token_response.get("scope") or " ".join(SCOPES),
    }
    _save_tokens(config, room_id, tokens)


def _check_token(room_id: str, key: str, config: Settings) -> str:
    """Classify a single account's token status.

    Ports google-workspace-pack's plugins/oauth_gate/__init__.py::_check_token
    exactly, keyed by account_key instead of the raw LINE user id.

    Args:
        room_id: Raw LINE room/user/group id (original case), used to
            locate this room's own tokens.json.
        key: account_key (lowercased room id) to check, inside that file.
        config: Application settings.

    Returns:
        "missing" (no usable token), "missing_scopes" (token present but
        REQUIRED_SCOPES not fully granted), or "ok".
    """
    if not config.room_google_tokens_path(room_id).exists():
        return "missing"
    try:
        tokens = _load_tokens(config, room_id)
        if key not in tokens:
            return "missing"
        token_data = tokens[key]
        expiry = token_data.get("expiry_date", 0)
        is_expired = time.time() * 1000 >= float(expiry) - 300_000  # type: ignore[arg-type]
        if is_expired and not token_data.get("refresh_token"):
            return "missing"
        granted = set(str(token_data.get("scope", "")).split())
        if not REQUIRED_SCOPES.issubset(granted):
            return "missing_scopes"
        return "ok"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.error(f"Failed to read Google tokens for account {key}: {exc}")
        return "missing"


def check_google_authorization(room_id: str, config: Settings) -> tuple[str, str | None]:
    """Decide whether a room's inbound message should be gated on Google auth.

    Ports google-workspace-pack's oauth_gate hook logic into a plain
    function the router calls directly (see module docstring for why the
    plugin form can't work here).

    Args:
        room_id: Raw LINE room/user/group id.
        config: Application settings.

    Returns:
        ("ok", None) — proceed normally.
        ("blocked", msg) — do not call the agent; deliver msg to the user instead.
        ("notice", msg) — proceed normally, but also push msg to the user
            (missing Drive scope; calendar/gmail still usable).
    """
    if not config.google_oauth_enabled or not config.GOOGLE_OAUTH_GATE:
        return "ok", None

    key = account_key(room_id)
    status = _check_token(room_id, key, config)
    # Deliberately the raw room_id, not key — oauth_start needs the original
    # case back to seed/locate the right data/<room_id>/google/ directory.
    auth_url = f"{config.GOOGLE_OAUTH_PUBLIC_URL}/oauth/start?user_id={room_id}"

    if status == "missing":
        return "blocked", _BLOCKED_MSG_TEMPLATE.format(auth_url=auth_url)
    if status == "missing_scopes":
        return "notice", _NOTICE_MSG_TEMPLATE.format(auth_url=auth_url)
    return "ok", None
