"""
Shared token manager for Google API MCP servers.
Reads/writes tokens from the shared /opt/google-workspace mount
and handles OAuth2 token refresh.
"""
import json
import os
import time
from pathlib import Path

import requests

# In-container shared-mount paths (see container_manager.py CONTAINER_GOOGLE_DIR
# and Settings.google_host_dir): tokens.json + both GCP credential files live
# on a single deployment-wide host directory bind-mounted read-write into
# every room's container at /opt/google-workspace, since Google accounts
# aren't per-room like the rest of a room's data. Paths are always explicit —
# never derive them from HOME/XDG defaults: this MCP subprocess runs as uid
# 10000 `hermes` (so /root is unreachable, mode 700), and the Hermes gateway
# sets XDG_CONFIG_HOME=/opt/data/.config, which would silently resolve any
# "default" config path per-room instead of to the shared mount. The env vars
# below are set in this MCP's mcp.manifest.yaml (visible/overridable in each
# room's config.yaml); the defaults only back them up.
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


def get_access_token(account_mode: str | None = None) -> str:
    if account_mode is None:
        account_mode = get_account_mode()

    tokens = load_all_tokens()
    if account_mode not in tokens:
        raise ValueError(
            f"No token found for account '{account_mode}'. "
            "Please authorize via LINE first."
        )

    token_data = tokens[account_mode]
    expiry_ms = token_data.get("expiry_date", 0)
    now_ms = int(time.time() * 1000)

    # Refresh if expired or expiring within 5 minutes
    if expiry_ms - now_ms < 300_000:
        refresh_tok = token_data.get("refresh_token", "")
        if not refresh_tok:
            raise ValueError(f"No refresh token for account '{account_mode}'")

        # Try web credentials first (LINE users), then installed (desktop users)
        new_token_data = None
        for creds_path in [WEB_CREDS_PATH, INSTALLED_CREDS_PATH]:
            try:
                creds = load_credentials_file(creds_path)
                result = refresh_token(refresh_tok, creds)
                if "access_token" in result:
                    new_token_data = result
                    break
            except Exception:
                continue

        if not new_token_data:
            raise RuntimeError(
                f"Failed to refresh token for account '{account_mode}'. "
                "Please re-authorize via LINE."
            )

        token_data["access_token"] = new_token_data["access_token"]
        token_data["expiry_date"] = (
            int(time.time() * 1000) + new_token_data["expires_in"] * 1000
        )
        tokens[account_mode] = token_data
        save_all_tokens(tokens)

    return token_data["access_token"]
