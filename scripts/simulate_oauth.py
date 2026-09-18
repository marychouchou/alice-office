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

What it deliberately does NOT do yet is (b): actually replay the pending
turn the way /oauth/callback would after a real authorization (the
core.resume_pending_auth + ChannelAdapter.resume path, plan §3.4 / §5 step
4). That plumbing doesn't exist yet, and no other agent's part of this
branch fully wires it up either — see the TODO in main() below for why
"just call the hook from here" doesn't work and what to do instead today.

Usage:
    uv run python scripts/simulate_oauth.py <room_id> <member_key>
    uv run python scripts/simulate_oauth.py <room_id> <member_key> \\
        --from-member-file data/<other_room>/google/members/<key>.json

Example (T3 of the plan's test table):
    uv run python scripts/simulate_oauth.py line_U_T10_ALICE line_u_t10_alice \\
        --from-member-file data/line_U_LOCAL_TEST/google/members/line_u_local_test.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from alice_office_router.config import get_settings
from alice_office_router.google_tokens import account_key, save_member_tokens

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
            "account_key 後當作這個房間這位成員的 token。不給就只做 (b) 的提示，"
            "適合房間/成員已經有 token 檔、只想重跑 resume 流程的情況。"
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Entry point: (a) deposit a token, (b) print the interim instruction for resume."""
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

    # (b) TODO once docs/google-auth-per-member-plan.md §5 step 4 lands
    # (core.resume_pending_auth + the ChannelAdapter.resume Protocol +
    # main.py registering the hook): replay whatever pending turn this
    # member left behind, the same way /oauth/callback does after a real
    # Google redirect:
    #
    #     await core.resume_pending_auth(args.room_id, args.member_key)
    #
    # Why this script can't just do that today: google_oauth.on_authorized
    # is a *module-level* variable, set only once, inside the actual running
    # router process (main.py's lifespan calling google_oauth.set_on_authorized).
    # `uv run python scripts/simulate_oauth.py` is a separate OS process with
    # its own fresh import of google_oauth — its on_authorized is always None,
    # so calling it here would call nothing, not the hook the real router
    # registered. There is also no HTTP endpoint yet that lets an outside
    # process ask the running router to replay a pending turn for
    # (room_id, member_key) — /oauth/callback is the only trigger, and it
    # needs a real Google `code`.
    print(
        "\n(b) step 4 落地前，router 還沒有能讓外部程序觸發「重跑 pending 訊息」的入口 —— "
        "on_authorized hook 只存在於正在跑的 router process 內部（main.py 的 lifespan 呼叫 "
        "google_oauth.set_on_authorized 註冊），這支腳本是另一個 process，import google_oauth "
        "拿到的是全新的空狀態，呼叫了也不會碰到真正在跑的 router；也還沒有 HTTP 入口能從外部觸發它。\n"
        "    step 4 之後：router 會提供從 /oauth/callback 之外觸發同一段 resume 的方式"
        "（規劃中的呼叫是 core.resume_pending_auth(room_id, member_key)），屆時這裡會補上。\n"
        "    現在的替代做法：(a) 完成後，直接重送一次原本被擋下的訊息（手機 LINE、"
        "scripts/test_webhook.py，或 curl API channel）即可 —— token 已經在磁碟上，"
        "這一回合會直接查到、不會再要求授權。"
    )


if __name__ == "__main__":
    main()
