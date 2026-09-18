"""Google OAuth authorization for LINE rooms.

Reimplements the semantics of the standalone google-workspace-pack's
oauth-server/oauth_server.py (Flask) and plugins/oauth_gate/__init__.py
(Hermes plugin) as FastAPI routes + a plain function this router calls
directly. Both must be reimplemented here rather than ported as-is:

- The oauth-server was a separate Flask process; this router is already the
  public HTTPS endpoint, so its routes become part of this app instead.
- oauth_gate cannot work as a Hermes plugin in this deployment: router->agent
  traffic uses Hermes's api_server platform (/v1/chat/completions), which
  bypasses the pre_gateway_dispatch hook the pack's plugin relied on. What is
  left of that gate therefore runs in the router itself
  (check_google_authorization) — since 2026-09-18 it no longer blocks a
  message, only notices a token that predates the Drive scope.

Token storage itself lives in google_tokens.py: tokens are written per
member under data/<room_id>/google/members/, with tokens.json a symlink the
router repoints at the current speaker. This module only exchanges codes and
hands the result to that store. Two different identifiers are both in play
and must not be conflated:

- room_id: the raw LINE room/user/group id (starts with uppercase U/C/R),
  used as-is for every filesystem path (DATA_DIR/room_id/google/...) — must
  keep its original case, or it silently diverges from the directory
  container_manager creates for the room's data/mcp/plugins.
- account_key(room_id): the same id lowercased, used as the dict key *inside*
  a member's token file and for GOOGLE_ACCOUNT_MODE, because
  @cocal/google-calendar-mcp validates that env var against
  /^[a-z0-9_-]{1,64}$/ (lowercase only).

_pending stores the raw room_id (not the account_key) precisely so
oauth_callback can recover the correct on-disk directory — together with the
member_key the link was issued to, because in a group every member authorizes
their own Google account and the callback has to write that member's own token
file. The key *inside* that file stays account_key(room_id) either way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from alice_office_router.config import Settings, get_settings
from alice_office_router.google_tokens import (
    account_key,
    check_member_token,
    load_member_tokens,
    migrate_legacy_tokens,
    save_member_tokens,
)
from alice_office_router.room_seed import ensure_google_seed

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# The token endpoint is Settings.GOOGLE_TOKEN_URL, not a constant here: local
# e2e runs point it at scripts/line_stub.py so the callback can be walked
# without Google (docs/testing-paths.md).

# How long to wait on Google's token endpoint before giving up. Without it the
# per-request AsyncClient would inherit httpx's no-timeout default, so a hung
# Google endpoint would block the OAuth callback indefinitely.
_TOKEN_EXCHANGE_TIMEOUT_SECONDS = 30.0


class _TokenResponse(BaseModel):
    """Minimal view of Google's OAuth token endpoint JSON response."""

    model_config = ConfigDict(extra="ignore")

    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    scope: str | None = None


# Scopes requested during the interactive OAuth consent flow.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]

# How long a state token started via /oauth/start stays valid, in seconds.
_PENDING_TTL_SECONDS = 600.0

# Process-local map of state -> (room_id, member_key, created_ts) — room_id
# kept in its original case (see module docstring). Mirrors the pack's
# in-memory PENDING dict: a router restart invalidates any in-flight
# authorization, and this is not shared across multiple router
# workers/processes (fine for the current single-worker deployment).
_pending: dict[str, tuple[str, str, float]] = {}

# The shape account_key() guarantees (and @cocal/google-calendar-mcp accepts).
# /oauth/start takes the member key straight off a URL, so it is checked
# against that shape before it can become a filename under members/.
_MEMBER_KEY_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

# Called after a member's token is stored, so the router can pick up whatever
# that member was waiting for: `core.resume_pending_auth`, registered by
# main.py's lifespan (docs/google-auth-per-member-plan.md §3.4). A hook rather
# than an import, so google_oauth never has to import core, which imports it.
on_authorized: Callable[[str, str], Awaitable[None]] | None = None

# Strong references to the in-flight hook tasks: asyncio holds only a weak
# one, so an unreferenced task can be garbage collected mid-await.
_hook_tasks: set[asyncio.Task[None]] = set()

_NOTICE_MSG_TEMPLATE = (
    "🔄 需要重新授權以啟用 Google Drive 功能。\n\n"
    "👉 點此重新授權（包含 Drive）：{auth_url}\n\n"
    "授權完成後即可使用 Drive 功能！"
)

# `https://line.me/R/nv/chat` opens LINE on its Chats tab — the only documented
# scheme that just returns the user to the app (there is no generic "open LINE"
# link, and `line://` is deprecated against app-takeover attacks):
# https://developers.line.biz/en/docs/line-login/using-line-url-scheme/
_LINE_CHATS_URL = "https://line.me/R/nv/chat"

_SUCCESS_HTML = f"""
    <html><body>
    <h2>授權成功。如果你剛才有問題等著處理，答案稍後會直接出現在 LINE。</h2>
    <p><a href="{_LINE_CHATS_URL}">回到 LINE</a></p>
    </body></html>
"""

oauth_router = APIRouter()


def set_on_authorized(hook: Callable[[str, str], Awaitable[None]] | None) -> None:
    """Register (or clear) the callback run after a member finishes authorizing.

    Args:
        hook: Coroutine function taking (room_id, member_key), or None to
            unregister. Its exceptions never reach the user's browser.
    """
    global on_authorized
    on_authorized = hook


def _purge_expired_pending() -> None:
    """Drop pending OAuth states older than _PENDING_TTL_SECONDS.

    Called on every access to _pending so it never grows unbounded across a
    long-lived process, without needing a background task.
    """
    now = time.monotonic()
    expired = [
        state
        for state, (_room, _member, created) in _pending.items()
        if now - created > _PENDING_TTL_SECONDS
    ]
    for state in expired:
        del _pending[state]


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
    member: str | None = None,
) -> RedirectResponse:
    """Start the Google OAuth consent flow for one member of a LINE room.

    Args:
        user_id: The raw LINE room/user/group id (query param), remembered
            as-is (original case) against the state — see module docstring
            for why this must not be lowercased here.
        member: The member key the resulting token belongs to (query param) —
            account_key(sender_id) in a group, account_key(room) in a 1:1
            room. Rejected unless it has the shape account_key produces,
            since it becomes a filename under the room's members/ directory.
        config: Application settings via dependency injection.

    Returns:
        A 302 redirect to Google's OAuth consent screen.

    Raises:
        HTTPException: 400 if user_id or member is missing/malformed, or if
            Google OAuth isn't configured.
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id")
    if not member or not _MEMBER_KEY_RE.fullmatch(member):
        raise HTTPException(status_code=400, detail="Missing or invalid member")
    if not config.google_oauth_enabled:
        raise HTTPException(status_code=400, detail="Google OAuth not configured")

    # This may be this room's very first contact with the filesystem: the link
    # can be clicked before the room's container (and thus its google/ dir)
    # exists, or while a warm-up is still building it. ensure_google_seed is
    # idempotent — a no-op if already seeded.
    ensure_google_seed(user_id, config)
    client_id, _ = _load_web_credentials(config, user_id)

    _purge_expired_pending()
    state = secrets.token_urlsafe(16)
    _pending[state] = (user_id, member, time.monotonic())

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": f"{config.PUBLIC_BASE_URL}/oauth/callback",
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
) -> _TokenResponse:
    """Exchange an OAuth authorization code for a token response.

    Args:
        code: Authorization code returned by Google.
        config: Application settings (for the redirect_uri and the token
            endpoint, which local e2e runs redirect to a stub).
        client_id: Web application OAuth client id.
        client_secret: Web application OAuth client secret.

    Returns:
        Google's parsed token endpoint response.
    """
    async with httpx.AsyncClient(timeout=_TOKEN_EXCHANGE_TIMEOUT_SECONDS) as client:
        response = await client.post(
            config.GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": f"{config.PUBLIC_BASE_URL}/oauth/callback",
                "grant_type": "authorization_code",
            },
        )
        return _TokenResponse.model_validate(response.json())


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
    room_id, member_key, _created = pending_entry

    client_id, client_secret = _load_web_credentials(config, room_id)
    token_response = await _exchange_code_for_token(code or "", config, client_id, client_secret)

    if token_response.access_token is None:
        logger.error(
            f"Google OAuth token exchange failed for room [{room_id}] "
            f"member [{member_key}]: {token_response}"
        )
        raise HTTPException(status_code=400, detail="OAuth failed")

    _store_token(config, room_id, member_key, token_response)
    _schedule_authorized(room_id, member_key)
    return HTMLResponse(content=_SUCCESS_HTML)


def _schedule_authorized(room_id: str, member_key: str) -> None:
    """Run the on_authorized hook in the background, if one is registered.

    The browser gets its success page as soon as the token is on disk; what
    the hook then does with it (re-running the question the member was in the
    middle of asking) can take a whole agent turn, and must never be able to
    fail this request.

    Args:
        room_id: Raw LINE room/user/group id (original case).
        member_key: The member whose token was just stored.
    """
    hook = on_authorized
    if hook is None:
        return
    task = asyncio.create_task(
        _run_authorized(hook, room_id, member_key), name=f"oauth-authorized:{room_id}"
    )
    _hook_tasks.add(task)
    task.add_done_callback(_hook_tasks.discard)


async def _run_authorized(
    hook: Callable[[str, str], Awaitable[None]], room_id: str, member_key: str
) -> None:
    """Await the on_authorized hook, turning any failure into a log line.

    Args:
        hook: The registered callback.
        room_id: Raw LINE room/user/group id.
        member_key: The member whose token was just stored.
    """
    try:
        await hook(room_id, member_key)
    except Exception as exc:
        # Deliberately broad: this runs detached on its own task, so an
        # unexpected exception type would otherwise surface only as asyncio's
        # "Task exception was never retrieved" at garbage-collection time.
        logger.error(f"on_authorized hook failed for room [{room_id}] member [{member_key}]: {exc}")


def _store_token(
    config: Settings, room_id: str, member_key: str, token_response: _TokenResponse
) -> None:
    """Merge a freshly exchanged token into one member's token file.

    The two identifiers are deliberately different: which *file* is written is
    the member's business, but the key *inside* it stays account_key(room_id),
    because that is the value each room's write-once config.yaml pinned as
    GOOGLE_ACCOUNT_MODE for its Google MCPs (google_tokens module docstring).

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case), used to
            locate this room's own google/ directory.
        member_key: Whose token file to write (account_key of the speaker; in
            a 1:1 room that is the room itself).
        token_response: Google's parsed token endpoint response.
    """
    # An upgraded room may still have its pre-member-store tokens.json here;
    # migrating first stops a later turn's migration from moving that stale
    # file over the token we are about to write.
    migrate_legacy_tokens(config, room_id)
    tokens = load_member_tokens(config, room_id, member_key)
    expires_in = token_response.expires_in or 0
    now_ms = int(time.time() * 1000)
    tokens[account_key(room_id)] = {
        "access_token": token_response.access_token,
        "refresh_token": token_response.refresh_token or "",
        "expiry_date": now_ms + expires_in * 1000,
        "token_type": "Bearer",
        "scope": token_response.scope or " ".join(SCOPES),
    }
    save_member_tokens(config, room_id, member_key, tokens)


def auth_url_for(config: Settings, room_id: str, member_key: str) -> str:
    """Build the authorization link for one member of one room.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id — deliberately *not* the
            account_key: oauth_start needs the original case back to locate
            the right data/<room_id>/google/ directory.
        member_key: The member the resulting token will belong to.

    Returns:
        The absolute /oauth/start URL to hand that member.
    """
    return f"{config.PUBLIC_BASE_URL}/oauth/start?user_id={room_id}&member={member_key}"


def check_google_authorization(
    room_id: str, member_key: str | None, config: Settings
) -> tuple[str, str | None]:
    """Report what this turn's speaker's Google token situation is.

    The gate stopped blocking messages on 2026-09-18: a speaker with no token
    reaches the agent like anybody else. Nothing here blocks or sends anything
    on its own — the two non-"ok" statuses are information the turn acts on:

    - "unauthorized" tells the caller to warn the *agent* before it tries a
      Google tool (core folds group_context.GOOGLE_AUTH_MISSING_HINT into the
      turn's system prompt). Without it the authorization link depends on the
      agent recognising whatever wording a failing Google tool happens to use,
      which the third-party calendar MCP ("Authentication tokens are no longer
      valid. Please restart the server to re-authenticate.") has already been
      seen to defeat: the agent told the user to re-authorize "in the settings"
      and never emitted the google-auth://request marker, so no link was issued.
    - "notice" is the one case no tool failure can surface at all — a token that
      works, so nothing fails, but predates the Drive scope.

    Args:
        room_id: Raw LINE room/user/group id.
        member_key: The speaker's account key, or None for a group speaker
            LINE would not identify. They can have no token at all, but there
            is no link to offer them either (auth_links), so they stay "ok".
        config: Application settings.

    Returns:
        ("ok", None) — nothing to say, proceed.
        ("unauthorized", None) — proceed, but the speaker has no usable token,
            so the turn should tell the agent up front.
        ("notice", msg) — proceed normally, but also push msg to the user
            (token present but missing the Drive scope; calendar/gmail work).
    """
    if not config.google_oauth_enabled or member_key is None:
        return "ok", None
    token_status = check_member_token(config, room_id, member_key)
    if token_status == "missing":
        return "unauthorized", None
    if token_status != "missing_scopes":
        return "ok", None
    auth_url = auth_url_for(config, room_id, member_key)
    return "notice", _NOTICE_MSG_TEMPLATE.format(auth_url=auth_url)
