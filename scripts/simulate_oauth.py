"""Local e2e helper: drop a real token into a room/member, skipping Google entirely.

docs/google-auth-per-member-plan.md §6b's test scenarios (T3 in particular)
need a way to make a room+member look "just authorized" without walking
through Google's consent screen every time — a real token copied from
another room's member file is good enough, since every room shares the same
GCP OAuth client credentials (see scripts/google_reauth.py's
DEFAULT_CREDENTIALS_PATH).

This script does exactly that one step:

    (a) optionally copy an existing member token file into
        data/<room_id>/google/members/<member_key>.json, re-keyed under THIS
        room's own account_key (google_tokens.py: a member file's single
        inner key is always account_key(room_id) of whichever room's
        directory it lives under — never the member_key, and never the
        source room's account_key).

    (b) walk the running router's real OAuth routes end to end, so everything
        /oauth/callback does after a genuine Google redirect actually happens:
        store the member token, fire the on_authorized hook, and resume the
        message the member parked (core.resume_pending_auth ->
        ChannelAdapter.resume, plan §3.4).

(b) needs one piece of local plumbing, because the callback insists on
exchanging its `code` at a token endpoint: start the router with
`GOOGLE_TOKEN_URL` pointing at `scripts/line_stub.py --google-token-file
<a member token file>` (Settings.GOOGLE_TOKEN_URL, docs/testing-paths.md).
The stub then answers the exchange with that file's token, so `code=fake`
is enough and no browser or Google account is involved. Without it the
callback fails at the exchange and this script says so.

The hook cannot be called from here instead: google_oauth.on_authorized is a
module-level variable set inside the *running router process* (main.py's
lifespan), and this script is a separate process whose fresh import of that
module has it at None. Driving the router's own HTTP routes is what makes
the real hook run.

Usage:
    uv run python scripts/simulate_oauth.py <room_id> <member_key>
    uv run python scripts/simulate_oauth.py <room_id> <member_key> \\
        --from-member-file data/<other_room>/google/members/<key>.json
    uv run python scripts/simulate_oauth.py <room_id> <member_key> --skip-callback

Example (T3 of the plan's test table):
    uv run python scripts/simulate_oauth.py line_U_T10_ALICE line_u_t10_alice \\
        --from-member-file data/line_U_LOCAL_TEST/google/members/line_u_local_test.json \\
        --router-url http://localhost:8010
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from alice_office_router.config import get_settings
from alice_office_router.google_tokens import account_key, save_member_tokens

DEFAULT_ROUTER_URL = "http://localhost:8000"
# The callback exchanges this for a token at Settings.GOOGLE_TOKEN_URL; the
# stub hands back a real token whatever the code says, so any value does.
FAKE_AUTH_CODE = "fake-authorization-code"
# The callback returns as soon as the token is on disk — the resume it kicks
# off is a detached task — so this only has to cover the token exchange.
_HTTP_TIMEOUT_SECONDS = 30.0

# The shape google_tokens.account_key() produces — member_key becomes a
# filename under the room's members/ directory (mirrors google_oauth.py's
# own _MEMBER_KEY_RE for /oauth/start's `member` query param).
_MEMBER_KEY_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def rekey_single_entry(source: dict[str, dict[str, object]], new_key: str) -> dict[str, object]:
    """Take a member file's one token entry and re-key it for a different room.

    A member file always holds exactly one entry, keyed account_key(room_id)
    of whichever room it was written for (google_tokens.py module docstring).
    Reusing a token from another room keeps its token DATA but must swap that
    key to this room's own account_key — the Google MCPs here look the
    account up strictly by that key (google_tokens.REQUIRED_SCOPES /
    check_member_token).

    Args:
        source: The source member file's parsed content.
        new_key: account_key(this room's room_id) — the key to store it under.

    Returns:
        A single-entry mapping {new_key: <token data>}.

    Raises:
        ValueError: If the source file doesn't hold exactly one entry (not
            shaped like a member file google_tokens.py itself writes).
    """
    if len(source) != 1:
        raise ValueError(
            f"expected exactly one entry in the source member file, found {len(source)}"
        )
    ((_old_key, token_data),) = source.items()
    return {new_key: token_data}


def build_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "本機模擬完成 Google 授權：把一份現成的 token 複製進某房間的某位成員檔，跳過瀏覽器。"
        )
    )
    parser.add_argument(
        "room_id", help="LINE room/user/group id（原始大小寫，同 data/<room_id>/ 目錄名）"
    )
    parser.add_argument(
        "member_key", help="要寫入的成員 key（account_key 形狀：^[a-z0-9_-]{1,64}$）"
    )
    parser.add_argument(
        "--from-member-file",
        type=Path,
        default=None,
        help=(
            "複製既有的成員 token 檔（例如另一個房間已授權過的 "
            "data/<room>/google/members/<key>.json）進來，重新 key 成這個房間的 "
            "account_key 後當作這個房間這位成員的 token。不給就跳過 (a)，"
            "適合房間/成員已經有 token 檔、只想重跑 resume 流程的情況。"
        ),
    )
    parser.add_argument(
        "--router-url",
        default=DEFAULT_ROUTER_URL,
        help=f"正在跑的 router base URL（預設 {DEFAULT_ROUTER_URL}）",
    )
    parser.add_argument(
        "--skip-callback",
        action="store_true",
        help="只做 (a) 寫檔，不去打 router 的 /oauth/start + /oauth/callback",
    )
    return parser.parse_args()


def _start_state(client: httpx.Client, router_url: str, room_id: str, member_key: str) -> str:
    """Run /oauth/start and pull the state token back out of the redirect.

    Args:
        client: HTTP client to use.
        router_url: Base URL of the running router.
        room_id: Raw LINE room/user/group id (original case).
        member_key: The member the link is issued to.

    Returns:
        The `state` query parameter the router put on its redirect to Google —
        the only thing /oauth/callback will accept.

    Raises:
        SystemExit: If the router did not answer with a redirect carrying a
            state (its 400s say why: OAuth not configured, bad member key).
    """
    response = client.get(
        f"{router_url}/oauth/start",
        params={"user_id": room_id, "member": member_key},
        follow_redirects=False,
    )
    if response.status_code != 302:
        print(f"[ERROR] /oauth/start 回了 {response.status_code}：{response.text[:200]}")
        raise SystemExit(1)
    location = response.headers.get("location", "")
    states = parse_qs(urlparse(location).query).get("state", [])
    if not states:
        print(f"[ERROR] /oauth/start 的 Location 沒有 state：{location[:200]}")
        raise SystemExit(1)
    return states[0]


def main() -> None:
    """Entry point: (a) deposit a token, (b) drive the router's OAuth routes."""
    args = build_args()
    if not _MEMBER_KEY_RE.fullmatch(args.member_key):
        print(f"[ERROR] member_key 格式不符 ^[a-z0-9_-]{{1,64}}$：{args.member_key!r}")
        raise SystemExit(1)

    config = get_settings()
    inner_key = account_key(args.room_id)

    if args.from_member_file is not None:
        if not args.from_member_file.exists():
            print(f"[ERROR] 來源檔不存在：{args.from_member_file}")
            raise SystemExit(1)
        source = json.loads(args.from_member_file.read_text(encoding="utf-8"))
        try:
            tokens = rekey_single_entry(source, inner_key)
        except ValueError as exc:
            print(f"[ERROR] {args.from_member_file} 不是一份成員 token 檔：{exc}")
            raise SystemExit(1) from exc
        save_member_tokens(config, args.room_id, args.member_key, tokens)
        dest = config.room_google_member_tokens_path(args.room_id, args.member_key)
        print(f"(a) 已複製 token：{args.from_member_file} -> {dest}（key 改為 '{inner_key}'）")
    else:
        dest = config.room_google_member_tokens_path(args.room_id, args.member_key)
        print(f"(a) 沒給 --from-member-file，跳過寫入 —— 假設 {dest} 已經存在。")

    if args.skip_callback:
        print("\n(b) --skip-callback：不打 router 的 OAuth 路由，結束。")
        return
    _run_callback(args.router_url, args.room_id, args.member_key)


def _run_callback(router_url: str, room_id: str, member_key: str) -> None:
    """Walk the router's own /oauth/start + /oauth/callback for one member.

    Args:
        router_url: Base URL of the running router.
        room_id: Raw LINE room/user/group id (original case).
        member_key: The member finishing authorization.

    Raises:
        SystemExit: If the router is unreachable or refuses either route.
    """
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            state = _start_state(client, router_url, room_id, member_key)
            response = client.get(
                f"{router_url}/oauth/callback",
                params={"code": FAKE_AUTH_CODE, "state": state},
            )
    except httpx.HTTPError as exc:
        print(f"[ERROR] 連不上 router {router_url}：{exc}")
        raise SystemExit(1) from exc

    if response.status_code != 200:
        print(
            f"[ERROR] /oauth/callback 回了 {response.status_code}：{response.text[:200]}\n"
            "        最常見原因：router 的 GOOGLE_TOKEN_URL 沒指到 stub 的假 token 端點。\n"
            "        起 stub 時加 --google-token-file <一份成員 token 檔>，router 端設\n"
            "        GOOGLE_TOKEN_URL=http://localhost:8099/token 再跑一次。"
        )
        raise SystemExit(1)

    print(
        f"(b) 已走完 router 的 /oauth/start + /oauth/callback（{router_url}）—— "
        "token 已寫入成員檔、on_authorized hook 已觸發。\n"
        "    如果這位成員有 pending 訊息（回覆裡拿到過授權連結），router 會在背景重跑它並"
        "**push** 答案；看 stub 的 data/_line_stub/requests.jsonl 有沒有新的 /v2/bot/message/push。"
    )


if __name__ == "__main__":
    main()
