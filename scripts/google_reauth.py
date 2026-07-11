"""Host-side one-shot Google re-authorization for dev machines with a browser.

Adapted from google-workspace-pack's reauth_all_scopes.py. Runs a local
HTTP server on localhost, opens the system browser to Google's OAuth
consent screen using the Desktop/Installed OAuth client credentials, and
stores the resulting token in this repo's shared tokens.json — the same
file the router's /oauth/callback route and every room's gmail/drive/
google-calendar MCP read from.

Usage:
    uv run python scripts/google_reauth.py <room_id>
    uv run python scripts/google_reauth.py U196d1445f7fe156eac44c02106f364ec

The room_id is lowercased before being stored (see the account_key rule in
alice_office_router.google_oauth) — @cocal/google-calendar-mcp's
GOOGLE_ACCOUNT_MODE validation rejects LINE's uppercase-prefixed room ids,
so the *same* lowercased key must be used everywhere: this script, the
router's oauth routes, and every MCP's env.
"""

from __future__ import annotations

import argparse
import http.server
import json
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_CREDENTIALS_PATH = Path("./data/_google/gcp-oauth.keys.installed.json")
DEFAULT_TOKENS_PATH = Path("./data/_google/tokens.json")

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
            self.wfile.write("<html><body><h2>授權成功，請回到終端機程式。</h2></body></html>".encode())
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


def save_token(tokens_path: Path, account_key: str, token_data: dict[str, object]) -> None:
    """Merge a freshly exchanged token into the shared tokens.json.

    Args:
        tokens_path: Path to the shared tokens.json file.
        account_key: Lowercased room id to store the token under.
        token_data: Google's token endpoint JSON response.
    """
    tokens_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, object] = {}
    if tokens_path.exists():
        existing = json.loads(tokens_path.read_text(encoding="utf-8"))

    expires_in = token_data.get("expires_in", 0)
    existing[account_key] = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expiry_date": int(time.time() * 1000) + int(expires_in) * 1000,  # type: ignore[call-overload]
        "token_type": "Bearer",
        "scope": SCOPES,
    }
    tokens_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"Token saved under account '{account_key}' in {tokens_path}")


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
    parser.add_argument("room_id", help="LINE room/user/group id to authorize (lowercased before storage)")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS_PATH,
        help=f"Path to the Desktop/Installed GCP OAuth client JSON (default: {DEFAULT_CREDENTIALS_PATH})",
    )
    parser.add_argument(
        "--tokens",
        type=Path,
        default=DEFAULT_TOKENS_PATH,
        help=f"Path to the shared tokens.json to write (default: {DEFAULT_TOKENS_PATH})",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point: run the interactive browser OAuth flow and save the token."""
    args = build_args()
    account_key = args.room_id.lower()
    if account_key != args.room_id:
        print(f"Note: normalizing account key to lowercase: '{args.room_id}' -> '{account_key}'")

    creds = load_installed_credentials(args.credentials)
    state = json.dumps({"nonce": time.time()})  # simple opaque state, single local user
    auth_url = build_auth_url(creds["client_id"], state)

    print(f"Opening authorization URL for account '{account_key}'...\n\n{auth_url}\n")
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

    save_token(args.tokens, account_key, token_data)
    print(f"\nSuccess! Account '{account_key}' now has all Google scopes.")


if __name__ == "__main__":
    main()
