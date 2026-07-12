"""Minimal terminal chat client for the router's local channel.

Talks to POST /channels/local/messages (see docs/channel-interface.md), so a
developer can converse with any room's Hermes agent without going through
LINE. Requires LOCAL_CHANNEL_TOKEN to be set on the router.

Usage:
    uv run python scripts/chat_tui.py [--url http://localhost:8000] \
        [--room local_dev] [--token <LOCAL_CHANNEL_TOKEN>]

Token resolution order: --token, $LOCAL_CHANNEL_TOKEN, then the repo's .env.
Type a message and press Enter; `exit` / `quit` / Ctrl-D leaves.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_env  # noqa: E402

# Generous: first contact with a room waits for its container to boot, and
# the agent itself can take up to hermes_client's 120s on top of that.
_REQUEST_TIMEOUT_SECONDS = 300.0


def _resolve_token(cli_token: str | None) -> str:
    """Resolve the local channel token from CLI arg, environment, or .env.

    Args:
        cli_token: Value of --token, if given.

    Returns:
        The token, or an empty string if none was found anywhere.
    """
    if cli_token:
        return cli_token
    env_token = os.environ.get("LOCAL_CHANNEL_TOKEN", "")
    if env_token:
        return env_token
    repo_env = load_env(Path(__file__).resolve().parent.parent / ".env")
    return repo_env.get("LOCAL_CHANNEL_TOKEN", "")


def _send_message(client: httpx.Client, url: str, room_id: str, token: str, text: str) -> list[str]:
    """Send one message to the local channel and return the lines to display.

    Args:
        client: Shared httpx client.
        url: Router base URL (no trailing slash).
        room_id: Target room id.
        token: LOCAL_CHANNEL_TOKEN value.
        text: Message text to send.

    Returns:
        Display lines: the delivered messages, or a single error/status line.
    """
    try:
        response = client.post(
            f"{url}/channels/local/messages",
            json={"room_id": room_id, "text": text},
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        return [f"[連線失敗] {exc}"]
    if response.status_code != 200:
        return [f"[HTTP {response.status_code}] {response.text}"]
    payload = response.json()
    messages: list[str] = payload.get("messages", [])
    if not messages:
        return [f"[無回覆，status={payload.get('status')}；詳情見 router log]"]
    return messages


def main() -> int:
    """Run the interactive chat loop.

    Returns:
        Process exit code (0 on normal exit, 1 on missing token).
    """
    parser = argparse.ArgumentParser(description="Chat with a room's Hermes agent via the router.")
    parser.add_argument("--url", default="http://localhost:8000", help="Router base URL")
    parser.add_argument("--room", default="local_dev", help="Target room id")
    parser.add_argument("--token", default=None, help="LOCAL_CHANNEL_TOKEN (overrides env/.env)")
    args = parser.parse_args()

    token = _resolve_token(args.token)
    if not token:
        print("找不到 LOCAL_CHANNEL_TOKEN（--token／環境變數／.env 皆未設定）。", file=sys.stderr)
        return 1

    url = args.url.rstrip("/")
    print(f"連線 {url}，房間 {args.room}（exit / quit / Ctrl-D 離開）")
    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text in {"exit", "quit"}:
                break
            for line in _send_message(client, url, args.room, token, text):
                print(f"agent> {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
