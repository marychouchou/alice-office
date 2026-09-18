"""Host-side one-shot Google re-authorization for dev machines with a browser.

Adapted from google-workspace-pack's reauth_all_scopes.py. Runs a local
HTTP server on localhost, opens the system browser to Google's OAuth
consent screen using the Desktop/Installed OAuth client credentials, and
stores the resulting token in one *member's* own token file under
data/<room_id>/google/members/ — the same per-member file the router's
/oauth/callback route writes and that room's gmail/drive/google-calendar
MCP read from, through the room's tokens.json symlink (see
alice_office_router.google_tokens module docstring; each room's Google data
is isolated, not shared across rooms).

Usage:
    uv run python scripts/google_reauth.py <room_id>
    uv run python scripts/google_reauth.py line_U196d1445f7fe156eac44c02106f364ec

    # 群組房間：指定要寫入哪個成員的 token 檔（預設是房間自己的 account_key，
    # 也就是舊行為 —— 只適合 1:1 房間，成員就是房間本身）
    uv run python scripts/google_reauth.py line_C4af4980629... --member u_t10_a

The room_id argument must be the *exact* room id used elsewhere for this
room (same case) — it becomes the data/<room_id>/ directory name, and
diverging case would silently create a second, empty directory instead of
authorizing the room the LINE webhook actually talks to. The token dict
*inside* the member file is still keyed by the lowercased account_key (see
alice_office_router.google_tokens.account_key) — @cocal/google-calendar-mcp's
GOOGLE_ACCOUNT_MODE validation rejects LINE's uppercase-prefixed room ids,
so that lowercased key must match what the router's oauth routes and every
MCP's env use, regardless of which member file it lives in.

--member does not choose which member file the router's tokens.json symlink
currently points at — that happens per-turn (google_tokens.select_member_tokens),
driven by who is actually speaking. This script only deposits a token; make
that member speak (or run scripts/simulate_oauth.py) to have the room pick
it up.
"""

from __future__ import annotations

import argparse
import http.server
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from alice_office_router.config import Settings, get_settings
from alice_office_router.google_tokens import account_key, load_member_tokens, save_member_tokens

# Deployment-level seed source (the operator's one-time drop location — see
# README「Google Workspace 整合」). Not room-specific: every room's own
# credentials copy starts as a copy of this same file.
DEFAULT_CREDENTIALS_PATH = Path("./data/_google/gcp-oauth.keys.installed.json")

# The shape google_tokens.account_key() produces (and @cocal/google-calendar-mcp
# accepts) — --member becomes a filename under the room's members/ directory,
# so it is checked against this before being trusted (mirrors google_oauth.py's
# own _MEMBER_KEY_RE for /oauth/start's `member` query param).
_MEMBER_KEY_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

SCOPES = " ".join(
    [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/drive",
    ]
)

CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/oauth/callback"

# Populated by CallbackHandler.do_GET when Google redirects back to us.
_received_code: str | None = None
_received_error: str | None = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Minimal local HTTP server that captures the OAuth redirect's code param."""

    def do_GET(self) -> None:  # noqa: N802 - required name by BaseHTTPRequestHandler
        """Handle the single expected GET /oauth/callback?code=...|error=... request."""
        global _received_code, _received_error
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if "code" in params:
            _received_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>授權成功，請回到終端機程式。</h2></body></html>".encode()
            )
        else:
            _received_error = params.get("error", ["unknown"])[0]
            self.send_response(400)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Suppress the default per-request access log line."""


def load_installed_credentials(path: Path) -> dict[str, str]:
    """Load a Desktop/Installed-type GCP OAuth client id/secret.

    Args:
        path: Path to the gcp-oauth.keys.installed.json file.

    Returns:
        The credentials mapping (client_id, client_secret, ...).

    Raises:
        FileNotFoundError: If path does not exist.
        KeyError: If the file has neither an "installed" nor "web" key.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    key = "installed" if "installed" in data else "web"
    return dict(data[key])


def build_auth_url(client_id: str, state: str) -> str:
    """Build the Google OAuth consent screen URL for the localhost redirect flow.

    Args:
        client_id: Desktop/Installed OAuth client id.
        state: Random CSRF state token.

    Returns:
        Full authorization URL string.
    """
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def exchange_code(code: str, creds: dict[str, str]) -> dict[str, object]:
    """Exchange an authorization code for an access/refresh token pair.

    Args:
        code: Authorization code received via the local callback server.
        creds: Desktop/Installed OAuth client id/secret.

    Returns:
        Google's token endpoint JSON response, decoded.
    """
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - fixed Google endpoint
        result: dict[str, object] = json.loads(response.read())
        return result


def ensure_room_credentials_copy(room_google_dir: Path, credentials_path: Path) -> None:
    """Copy the Desktop/Installed credentials file into a room's own google/ dir, once.

    Mirrors room_seed.ensure_google_seed's write-once semantics: a
    room's calendar MCP reads its own per-room mount, not any shared
    location, so this room needs its own copy of the credentials file for
    token refresh to work after this script hands off to the running
    container.

    Args:
        room_google_dir: This room's own google/ directory
            (Settings.room_google_dir(room_id)).
        credentials_path: Source Desktop/Installed credentials file to copy from.
    """
    dest = room_google_dir / "gcp-oauth.keys.installed.json"
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(credentials_path.read_bytes())
    print(f"Seeded room credentials: {credentials_path} -> {dest}")


def save_token(
    config: Settings, room_id: str, member_key: str, token_data: dict[str, object]
) -> None:
    """Merge a freshly exchanged token into one member's own Google token file.

    Goes through google_tokens.save_member_tokens (temp-file-then-rename)
    instead of writing tokens.json directly — tokens.json is now a symlink
    the router repoints at whichever member is currently speaking
    (google_tokens.select_member_tokens), so it must never be written to as
    a plain file.

    Args:
        config: Application settings (resolves data/<room_id>/google/members/).
        room_id: Raw LINE room/user/group id (original case).
        member_key: Which member's token file to write (see --member).
        token_data: Google's token endpoint JSON response.
    """
    inner_key = account_key(room_id)
    tokens = load_member_tokens(config, room_id, member_key)
    expires_in = token_data.get("expires_in", 0)
    tokens[inner_key] = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expiry_date": int(time.time() * 1000) + int(expires_in) * 1000,  # type: ignore[call-overload]
        "token_type": "Bearer",
        "scope": SCOPES,
    }
    save_member_tokens(config, room_id, member_key, tokens)
    path = config.room_google_member_tokens_path(room_id, member_key)
    print(f"Token saved under account '{inner_key}' in member file {path}")


def _wait_for_callback(timeout: float = 120.0) -> None:
    """Run the local callback server until it receives one request or times out.

    Args:
        timeout: Maximum seconds to wait for the browser redirect.
    """
    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    print("Waiting for authorization callback (timeout: 120s)...")
    deadline = time.time() + timeout
    while thread.is_alive() and time.time() < deadline:
        time.sleep(0.5)


def build_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="One-shot Google re-authorization (Calendar + Gmail + Drive) for dev machines."
    )
    parser.add_argument(
        "room_id",
        help=(
            "LINE room/user/group id to authorize — must match the exact case used "
            "elsewhere for this room (it becomes the data/<room_id>/ directory name); "
            "lowercased only for the token dict key inside the member file"
        ),
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS_PATH,
        help=f"Path to the Desktop/Installed GCP OAuth client JSON (default: {DEFAULT_CREDENTIALS_PATH})",
    )
    parser.add_argument(
        "--member",
        default=None,
        help=(
            "Which member's token file to write, under data/<room_id>/google/members/ "
            "(default: account_key(room_id) — today's 1:1-room behavior, where the "
            "member IS the room). Must match ^[a-z0-9_-]{1,64}$ — it becomes a filename."
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Entry point: run the interactive browser OAuth flow and save the token."""
    args = build_args()
    inner_key = account_key(args.room_id)
    if inner_key != args.room_id:
        print(f"Note: normalizing account key to lowercase: '{args.room_id}' -> '{inner_key}'")

    member_key = args.member or inner_key
    if not _MEMBER_KEY_RE.fullmatch(member_key):
        print(f"[ERROR] --member 格式不符 ^[a-z0-9_-]{{1,64}}$：{member_key!r}")
        raise SystemExit(1)

    config = get_settings()

    creds = load_installed_credentials(args.credentials)
    state = json.dumps({"nonce": time.time()})  # simple opaque state, single local user
    auth_url = build_auth_url(creds["client_id"], state)

    print(
        f"Opening authorization URL for room '{args.room_id}' member '{member_key}'...\n\n{auth_url}\n"
    )
    webbrowser.open(auth_url)
    _wait_for_callback()

    if _received_error:
        print(f"Authorization failed: {_received_error}")
        raise SystemExit(1)
    if not _received_code:
        print("Timeout waiting for authorization.")
        raise SystemExit(1)

    print("Authorization code received. Exchanging for token...")
    token_data = exchange_code(_received_code, creds)
    if "access_token" not in token_data:
        print(f"Token exchange failed: {token_data}")
        raise SystemExit(1)

    save_token(config, args.room_id, member_key, token_data)
    ensure_room_credentials_copy(config.room_google_dir(args.room_id), args.credentials)
    print(f"\nSuccess! Member '{member_key}' (account '{inner_key}') now has all Google scopes.")


if __name__ == "__main__":
    main()
