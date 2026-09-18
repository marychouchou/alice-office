"""
Shared token manager for Google API MCP servers.
Reads/writes tokens from this room's own /opt/google-workspace mount
and handles OAuth2 token refresh.
"""
import json
import os
import time
from pathlib import Path

import requests

# In-container mount paths (see container_manager.py CONTAINER_GOOGLE_DIR and
# Settings.room_google_host_dir): tokens.json + both GCP credential files
# live under this room's own host directory (data/<room_id>/google/),
# bind-mounted read-write at /opt/google-workspace — isolated per room, not
# shared across the deployment. Paths are always explicit — never derive
# them from HOME/XDG defaults: this MCP subprocess runs as uid 10000
# `hermes` (so /root is unreachable, mode 700), and the Hermes gateway sets
# XDG_CONFIG_HOME=/opt/data/.config, which would silently resolve any
# "default" config path elsewhere per-room. The env vars below are set in
# this MCP's mcp.manifest.yaml (visible/overridable in each room's
# config.yaml); the defaults only back them up.
TOKEN_PATH = Path(os.environ.get("GOOGLE_TOKENS_PATH", "/opt/google-workspace/tokens.json"))
WEB_CREDS_PATH = Path(
    os.environ.get("GOOGLE_WEB_CREDS_PATH", "/opt/google-workspace/gcp-oauth.keys.json")
)
INSTALLED_CREDS_PATH = Path(
    os.environ.get(
        "GOOGLE_INSTALLED_CREDS_PATH", "/opt/google-workspace/gcp-oauth.keys.installed.json"
    )
)
TOKEN_URI = "https://oauth2.googleapis.com/token"


def load_credentials_file(path: Path) -> dict:
    with open(path) as f:
        d = json.load(f)
    key = "web" if "web" in d else "installed"
    return d[key]


def get_account_mode() -> str:
    return os.environ.get("GOOGLE_ACCOUNT_MODE", "normal")


def load_all_tokens() -> dict:
    if not TOKEN_PATH.exists():
        return {}
    with open(TOKEN_PATH) as f:
        return json.load(f)


def save_all_tokens(tokens: dict):
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        json.dump(tokens, f, indent=2)


def refresh_token(refresh_token_str: str, creds: dict) -> dict:
    resp = requests.post(TOKEN_URI, data={
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": refresh_token_str,
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()


# What the router swaps for this speaker's own authorization link, on its way
# out to the chat room (see alice_office_router/auth_links.py and
# docs/google-auth-per-member-plan.md §3.3). The MCP cannot build that link
# itself: it knows neither its room id nor the router's public URL. So every
# "you are not authorized" error carries this placeholder plus an instruction
# to paste it verbatim — an invented URL would be a dead end for the user.
AUTH_REQUEST_MARKER = "google-auth://request"


def auth_required_message(account_mode: str) -> str:
    """Build the error text an unauthorized tool call returns to the agent.

    Args:
        account_mode: The account key this room's MCPs are pinned to. Kept
            out of the user-facing sentence (it is a room id, meaningless to
            the reader) and appended in a trailing parenthesis for the log.

    Returns:
        One line the agent can act on: the marker, and what to do with it.
    """
    return (
        "No Google authorization for this speaker. Reply to the user with the exact "
        f"text {AUTH_REQUEST_MARKER} on its own line so the router can turn it into "
        "their personal authorization link. Do not invent a URL. "
        f"(account '{account_mode}')"
    )


def refresh_with_any_credentials(refresh_tok: str) -> dict | None:
    """Try each credentials file in turn until one of them refreshes the token.

    Web credentials come first (a LINE user authorized through the router's
    /oauth/start), then the Desktop/Installed ones (a developer who authorized
    on their own machine).

    Args:
        refresh_tok: The stored refresh token.

    Returns:
        Google's token endpoint response, or None when neither credentials
        file produced an access token.
    """
    for creds_path in [WEB_CREDS_PATH, INSTALLED_CREDS_PATH]:
        try:
            creds = load_credentials_file(creds_path)
            result = refresh_token(refresh_tok, creds)
        except Exception:
            continue
        if "access_token" in result:
            return result
    return None


def get_access_token(account_mode: str | None = None) -> str:
    """Return a usable access token for this room's pinned Google account.

    Args:
        account_mode: Account key to read from the token file; defaults to
            GOOGLE_ACCOUNT_MODE.

    Returns:
        A non-expired access token, refreshing and rewriting the token file
        first when the stored one is within five minutes of expiring.

    Raises:
        ValueError: If this speaker has no stored token, or has one that can
            no longer be refreshed. Both carry AUTH_REQUEST_MARKER, so the
            agent's reply becomes an authorization link.
    """
    if account_mode is None:
        account_mode = get_account_mode()

    tokens = load_all_tokens()
    if account_mode not in tokens:
        raise ValueError(auth_required_message(account_mode))

    token_data = tokens[account_mode]
    expiry_ms = token_data.get("expiry_date", 0)
    now_ms = int(time.time() * 1000)

    # Refresh if expired or expiring within 5 minutes
    if expiry_ms - now_ms >= 300_000:
        return token_data["access_token"]

    refresh_tok = token_data.get("refresh_token", "")
    new_token_data = refresh_with_any_credentials(refresh_tok) if refresh_tok else None
    if not new_token_data:
        raise ValueError(auth_required_message(account_mode))

    token_data["access_token"] = new_token_data["access_token"]
    token_data["expiry_date"] = (
        int(time.time() * 1000) + new_token_data["expires_in"] * 1000
    )
    tokens[account_mode] = token_data
    save_all_tokens(tokens)

    return token_data["access_token"]
