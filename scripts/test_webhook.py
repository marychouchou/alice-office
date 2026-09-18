"""Manual end-to-end test: send a fake LINE webhook to the local router.

前置條件
--------
Router 必須先啟動：
    uv run uvicorn alice_office_router.main:app --host 0.0.0.0 --port 8000

測試指令一覽
------------

[1] 最基本測試 — 確認整條路（簽章→容器建立→LLM）是否通
    uv run python scripts/test_webhook.py

[2] 自訂訊息內容
    uv run python scripts/test_webhook.py --text "今天天氣如何？"

[3] 換一個 userId → 驗證會建立全新的隔離容器、資料不互通
    uv run python scripts/test_webhook.py --user-id "U_TEST_002" --text "我是第二個用戶"

[4] 測試技能建立功能 — 驗證 LLM 回應包含 <create_skill> 且寫入磁碟
    uv run python scripts/test_webhook.py --text "請建立一個叫做 weather_report 的技能，功能是查詢天氣"

    執行後確認技能是否寫入：
    ls data/line_<userId>/skills/

[5] 測試 group 來源（groupId 路由）
    uv run python scripts/test_webhook.py --group-id "C_GROUP_001" --text "大家好"

[6] 只 ping router，確認服務活著（不觸發 LLM，也不建容器）
    uv run python scripts/test_webhook.py --ping

[7] 列出目前所有正在跑的 hermes 容器
    uv run python scripts/test_webhook.py --list-containers

[8] LLM 回應較慢時，延長等待時間（預設 8 秒）
    uv run python scripts/test_webhook.py --text "寫一首關於秋天的詩" --wait 20

[8b] 第一次對全新房間送訊息常會撞到 client 端 ReadTimeout（建容器 + 等 Hermes
     health check最多 60 秒，預設 --send-timeout 65 秒理論上夠用，仍嫌不夠可再調高）
    uv run python scripts/test_webhook.py --user-id "U_NEW_ROOM" --send-timeout 120

[9] 測試貼圖事件（驗證佔位文字轉換，不需要真實 LINE 媒體）
    uv run python scripts/test_webhook.py --sticker

[10] 測試位置事件（驗證佔位文字轉換）
    uv run python scripts/test_webhook.py --location

[11] 測試圖片事件（驗證下載並落地到 data/line_<userId>/incoming/）
    uv run python scripts/test_webhook.py --image-message-id "<真實 LINE messageId>"

    注意：router 會用真實的 LINE Content API 下載這個 messageId 的內容，所以
    要傳一個「真的」曾經在你的 LINE OA 收到過的圖片訊息 messageId（可以從真實
    使用者傳圖時的 container log 或 router log 找到），假造的 ID 會下載失敗
    （router 會記錄錯誤並略過該事件，這是預期行為，不代表程式壞掉）。
    成功時可確認：
      ls data/line_<userId>/incoming/

[12] 群組訊息 + 指定發話者 userId + @mention（模擬群組裡一個已加好友的成員
     問問題並點名 bot；docs/google-auth-per-member-plan.md §6b T5）
    uv run python scripts/test_webhook.py --group-id "C_T10_GROUP" --sender-id "U_T10_A" --mention --text "明天有什麼會"
    uv run python scripts/test_webhook.py --group-id "C_T10_GROUP" --sender-id "U_T10_B" --mention --text "幫我看一下信件"

[13] 群組訊息 + @mention，但不給 --sender-id（模擬 LINE 沒提供身分的匿名發話
     者，例如還沒加 OA 好友的群組成員；T7）
    uv run python scripts/test_webhook.py --group-id "C_T10_GROUP" --mention --text "明天有什麼會"

[14] 送 follow／join 事件（沒有 message，只驗證暖機觸發點；T13）
    uv run python scripts/test_webhook.py --event follow --user-id "U_T10_NEW"
    uv run python scripts/test_webhook.py --event join --group-id "C_T10_NEW"

看 log 的方式
-------------
即時追蹤特定容器的 log（容器名是 hermes_ + 房間 key，房間 key 是 line_ + LINE ID）：
    docker logs -f hermes_line_<userId>

例如預設測試的容器：
    docker logs -f hermes_line_U_LOCAL_TEST

Log 判讀重點（container log，看 hermes agent 有沒有收到訊息）：
    POST /v1/chat/completions 200   → hermes agent api_server 收到並回覆了訊息

Log 判讀重點（router 自己的 terminal/log，看 LLM 呼叫與 LINE 回覆有沒有成功）：
    Hermes agent request failed     → 呼叫 hermes agent 失敗，需要排查
    Failed to download LINE ... content → LINE Content API 下載媒體失敗（假 messageId 屬預期行為）
    LINE reply token rejected       → reply token 過期/已用過，已自動 fallback 到 Push（正常行為）
    Failed to push LINE reply       → LINE Push API 失敗（假 room_id/token 屬預期行為）
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).parent.parent / ".env"
# Legacy alias of the canonical LINE webhook path /webhooks/line; kept working
# while the LINE OA console still points here (see channel-interface-plan.md).
# Overridable because the port-8000 router is often the docker-compose one
# while the code under test runs on the host on another port: set the env var
# ROUTER_URL (whole URL, path included) to aim somewhere else.
DEFAULT_ROUTER_URL = "http://localhost:8000/webhook"
ROUTER_URL = os.environ.get("ROUTER_URL", DEFAULT_ROUTER_URL)
WAIT_SECONDS = 8  # how long to wait for LLM to respond before checking logs
# POST /webhook 本身的 client 端逾時。router 立刻回 200，但第一次對一個全新房間
# 送訊息時，建容器 + 等 Hermes health check 最多可能到 60 秒（見 README），這條
# 同步阻塞的路徑會延遲 event loop 把回應完整送出，所以這裡故意抓比 60 秒長一點。
SEND_TIMEOUT_SECONDS = 65.0


def load_env(path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file, ignoring comments and blanks.

    Args:
        path: Path to the .env file.

    Returns:
        Dictionary of env var names to values.
    """
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# LINE signature
# ---------------------------------------------------------------------------


def sign(body: str, secret: str) -> str:
    """Compute the LINE HMAC-SHA256 signature for a webhook body.

    Args:
        body: Raw JSON string of the webhook body.
        secret: LINE Channel Secret.

    Returns:
        Base64-encoded signature string.
    """
    digest = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


# ---------------------------------------------------------------------------
# Webhook body builders
# ---------------------------------------------------------------------------


def make_user_message(user_id: str, text: str) -> str:
    """Build a LINE webhook body simulating a user text message.

    Args:
        user_id: Simulated LINE userId.
        text: Message text.

    Returns:
        JSON string.
    """
    payload = {
        "events": [
            {
                "type": "message",
                "source": {"type": "user", "userId": user_id},
                "message": {"type": "text", "text": text},
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


# Fake @mention label prepended to the text when --mention is set. LINE's
# real mention text is whatever the OA's display name is; only `isSelf`
# (never the label's characters) drives Event.mention_is_self / _is_addressed
# (channels/line/events.py), so any plain-ASCII label is fine for a fake body
# — ASCII also keeps the index/length math trivial (1 UTF-16 unit per char).
_MENTION_LABEL = "@bot"


def _mention_message(text: str) -> tuple[str, dict[str, object]]:
    """Prefix `text` with a self-@mention and build LINE's `mention` object.

    Mirrors channels/line/events.py's Mention/Mentionee shape: `isSelf: true`
    is the one field `Event.mention_is_self` checks; `index`/`length` count
    UTF-16 code units over the final text (see `_strip_self_mentions`), both
    trivial here since `_MENTION_LABEL` is plain ASCII at position 0.

    Args:
        text: The message text the mention prefix is added in front of.

    Returns:
        (text with the "@bot " prefix, the `message.mention` object).
    """
    full_text = f"{_MENTION_LABEL} {text}"
    mention: dict[str, object] = {
        "mentionees": [
            {
                "index": 0,
                "length": len(_MENTION_LABEL),
                "type": "user",
                "userId": "U" + "0" * 32,  # fake bot userId; only isSelf is read
                "isSelf": True,
            }
        ]
    }
    return full_text, mention


def make_group_message(
    group_id: str, text: str, *, sender_id: str = "", mention: bool = False
) -> str:
    """Build a LINE webhook body simulating a group text message.

    Args:
        group_id: Simulated LINE groupId.
        text: Message text (before any --mention prefix is applied).
        sender_id: If set, added as `source.userId` next to `groupId` — LINE's
            real shape for a message from a group member who has added the OA
            as a friend. Left empty, the source carries no userId (LINE's
            shape for a member who hasn't — an unidentified/anonymous
            speaker, see `channels/line/events.Event.sender_id`).
        mention: If True, prefix the text with a self-@mention of the bot and
            attach the matching `message.mention` object, so
            `LineAdapter._is_addressed` treats this message as addressed.

    Returns:
        JSON string.
    """
    source: dict[str, object] = {"type": "group", "groupId": group_id}
    if sender_id:
        source["userId"] = sender_id

    message: dict[str, object] = {"type": "text", "text": text}
    if mention:
        full_text, mention_obj = _mention_message(text)
        message["text"] = full_text
        message["mention"] = mention_obj

    payload = {"events": [{"type": "message", "source": source, "message": message}]}
    return json.dumps(payload, ensure_ascii=False)


def make_sticker_message(user_id: str) -> str:
    """Build a LINE webhook body simulating a sticker message.

    Args:
        user_id: Simulated LINE userId.

    Returns:
        JSON string.
    """
    payload = {
        "events": [
            {
                "type": "message",
                "source": {"type": "user", "userId": user_id},
                "message": {
                    "type": "sticker",
                    "packageId": "446",
                    "stickerId": "1988",
                    "keywords": ["Happy", "Fun"],
                },
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def make_location_message(user_id: str) -> str:
    """Build a LINE webhook body simulating a location message.

    Args:
        user_id: Simulated LINE userId.

    Returns:
        JSON string.
    """
    payload = {
        "events": [
            {
                "type": "message",
                "source": {"type": "user", "userId": user_id},
                "message": {
                    "type": "location",
                    "title": "台北車站",
                    "address": "100台灣台北市中正區北平西路3號",
                    "latitude": 25.0478,
                    "longitude": 121.5170,
                },
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def make_image_message(user_id: str, message_id: str) -> str:
    """Build a LINE webhook body simulating an image message.

    The router calls the real LINE Content API to download `message_id`'s
    binary content, so this only actually saves a file under
    `data/<user_id>/incoming/` when `message_id` is a real image messageId
    captured from live LINE traffic. A synthetic ID will fail to download —
    the router logs the error and drops the event, which is expected.

    Args:
        user_id: Simulated LINE userId.
        message_id: LINE message id to request content for.

    Returns:
        JSON string.
    """
    payload = {
        "events": [
            {
                "type": "message",
                "source": {"type": "user", "userId": user_id},
                "message": {"type": "image", "id": message_id},
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def make_verification_ping() -> str:
    """Build an empty-events ping (LINE sends this when you save Webhook URL).

    Returns:
        JSON string.
    """
    return json.dumps({"events": []})


def make_follow_event(user_id: str) -> str:
    """Build a LINE webhook body simulating a "follow" event (added as a friend).

    No `message` — this only exercises event-level dispatch (e.g. the
    follow/join -> warm_room trigger of docs/google-auth-per-member-plan.md
    §3.5/§5 step 5). Shape per LINE's FollowEvent schema (line-openapi
    webhook.yml): a message-less event whose only event-specific field is
    `replyToken` (plus the common `mode`/`timestamp`/`deliveryContext`
    envelope fields every event carries).

    Args:
        user_id: Simulated LINE userId who just added the OA as a friend.

    Returns:
        JSON string.
    """
    payload = {
        "events": [
            {
                "type": "follow",
                "mode": "active",
                "timestamp": int(time.time() * 1000),
                "source": {"type": "user", "userId": user_id},
                "webhookEventId": "stub-follow-event",
                "deliveryContext": {"isRedelivery": False},
                "replyToken": "stub-reply-token-follow",
                "follow": {"isUnblocked": False},
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def make_join_event(group_id: str) -> str:
    """Build a LINE webhook body simulating a "join" event (bot added to a group).

    Shape per LINE's JoinEvent schema (line-openapi webhook.yml): a
    message-less event whose only event-specific field is `replyToken`.

    Args:
        group_id: Simulated LINE groupId the OA was just added to.

    Returns:
        JSON string.
    """
    payload = {
        "events": [
            {
                "type": "join",
                "mode": "active",
                "timestamp": int(time.time() * 1000),
                "source": {"type": "group", "groupId": group_id},
                "webhookEventId": "stub-join-event",
                "deliveryContext": {"isRedelivery": False},
                "replyToken": "stub-reply-token-join",
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def container_name(room_id: str) -> str:
    """Return the expected container name for a given LINE id.

    Containers are named after the *room key* core routes on, not the bare
    LINE id: `hermes_line_<userId|groupId|roomId>` (container_manager.
    _container_name over channels/line/events.py's `line_` prefix). Everything
    this script is given on the command line is a bare LINE id, so the prefix
    is added here — without it every container check printed 不存在 ❌.

    Args:
        room_id: The LINE userId / groupId / roomId.

    Returns:
        Docker container name string.
    """
    return f"hermes_line_{room_id}"


def get_container_status(room_id: str) -> str | None:
    """Check if a container exists for the given room_id.

    Args:
        room_id: The LINE userId / groupId / roomId.

    Returns:
        Container status string, or None if container does not exist.
    """
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", container_name(room_id)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_container_logs(room_id: str, tail: int = 30) -> str:
    """Fetch recent logs from the hermes container for a room.

    Args:
        room_id: The LINE userId / groupId / roomId.
        tail: Number of log lines to fetch from the end.

    Returns:
        Log output as a string.
    """
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), container_name(room_id)],
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def list_hermes_containers() -> None:
    """Print all running hermes_* containers."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "name=hermes_",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
        ],
        capture_output=True,
        text=True,
    )
    print(result.stdout or "（沒有 hermes 容器在運行）")


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------


def send_webhook(body: str, secret: str, timeout: float = SEND_TIMEOUT_SECONDS) -> tuple[int, str]:
    """POST a signed webhook body to the local router.

    Args:
        body: Raw JSON string to send.
        secret: LINE Channel Secret for signing.
        timeout: Client-side read timeout in seconds.

    Returns:
        Tuple of (HTTP status code, response text).
    """
    sig = sign(body, secret)
    with httpx.Client() as client:
        resp = client.post(
            ROUTER_URL,
            content=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-line-signature": sig,
            },
            timeout=timeout,
        )
    return resp.status_code, resp.text


def run_test(
    secret: str,
    body: str,
    room_id: str,
    label: str,
    wait: int = WAIT_SECONDS,
    send_timeout: float = SEND_TIMEOUT_SECONDS,
) -> None:
    """Send one webhook and report results.

    Args:
        secret: LINE Channel Secret.
        body: JSON webhook body string.
        room_id: Expected room_id (for container lookup).
        label: Test label shown in output.
        wait: Seconds to wait before checking logs.
        send_timeout: Client-side read timeout for the POST /webhook call.
    """
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Target room : {room_id}")
    print(f"  Body        : {body[:80]}{'...' if len(body) > 80 else ''}")

    # Send
    try:
        status, text = send_webhook(body, secret, timeout=send_timeout)
    except httpx.ConnectError:
        print("\n  [ERROR] Router に接続できません。サーバーが起動していますか？")
        print("  → uv run uvicorn alice_office_router.main:app --host 0.0.0.0 --port 8000")
        return
    except httpx.ReadTimeout:
        print(f"\n  [ERROR] {send_timeout:.0f} 秒內沒有收到 router 回應。")
        print("  → 先看 router log（host 模式看 terminal，container 模式")
        print("     `docker compose logs webhook_router`）：如果有看到")
        print("     'POST /webhook' 200 與 '/v1/chat/completions' 200，代表其實")
        print("     有處理成功，只是 client 端等太久——重送一次（房間容器已建立，")
        print("     warm start）通常就會秒回。真的常態性 timeout 才需要調高")
        print("     --send-timeout。")
        return

    print(f"\n  Router response : {status} {text}")

    if status != 200:
        print("  [FAIL] Router returned non-200.")
        return

    # Wait for async processing
    print(f"\n  LLM の処理を {wait} 秒待ちます...", flush=True)
    for i in range(wait):
        time.sleep(1)
        print(f"  {i + 1}/{wait}", end="\r", flush=True)
    print()

    # Container check
    container_status = get_container_status(room_id)
    if container_status:
        print(f"\n  Container [{container_name(room_id)}]: {container_status} ✅")
    else:
        print(f"\n  Container [{container_name(room_id)}]: 不存在 ❌")
        return

    # Logs
    print("\n  --- Container logs (tail 30) ---")
    logs = get_container_logs(room_id)
    print(logs)

    # Heuristic checks — router calls the container's api_server, container
    # never touches LINE directly, so look for the chat completion request.
    if "/v1/chat/completions" in logs:
        print("  [PASS] Hermes agent api_server 收到了訊息 ✅")
    else:
        print("  [WARN] Log 中找不到 /v1/chat/completions 請求")

    print(
        "\n  [INFO] LINE Push 是 router 自己呼叫的，不會出現在 container log，"
        "\n         要看 router 這邊的 terminal/log 檔，找："
        "\n           'Hermes agent request failed' → LLM 呼叫失敗"
        "\n           'Failed to push LINE reply'    → LINE Push 失敗（假 room_id/token 屬預期）"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(description="LINE Webhook End-to-End Tester")
    parser.add_argument("--text", default="你好，測試一下", help="訊息內容")
    parser.add_argument("--user-id", default="U_LOCAL_TEST", help="模擬的 LINE userId")
    parser.add_argument("--group-id", default="", help="改用 groupId（設定後忽略 --user-id）")
    parser.add_argument(
        "--sender-id",
        default="",
        help=(
            "群組訊息的發話者 userId（僅搭配 --group-id 有效）：LINE 真實群組訊息會把它放在 "
            "source.userId，跟 groupId 並列；不給就是 LINE 沒提供身分的匿名發話者"
        ),
    )
    parser.add_argument(
        "--mention",
        action="store_true",
        help="群組訊息前面加上對 bot 的 @mention，讓 _is_addressed 判定為真（僅搭配 --group-id 有效）",
    )
    parser.add_argument(
        "--event",
        choices=["follow", "join"],
        default="",
        help="送一個沒有 message 的事件：follow（source=user，用 --user-id）"
        "或 join（source=group，用 --group-id）",
    )
    parser.add_argument("--wait", type=int, default=WAIT_SECONDS, help="等待 LLM 回應的秒數")
    parser.add_argument(
        "--send-timeout",
        type=float,
        default=SEND_TIMEOUT_SECONDS,
        help=f"POST /webhook 的 client 端逾時秒數（預設 {SEND_TIMEOUT_SECONDS:.0f}，"
        "蓋過房間第一次建容器的冷啟動時間）",
    )
    parser.add_argument("--list-containers", action="store_true", help="列出所有 hermes 容器後離開")
    parser.add_argument("--ping", action="store_true", help="只發一個空 events ping 測 router 連線")
    parser.add_argument(
        "--sticker",
        action="store_true",
        help="送出貼圖事件（測試佔位文字轉換，不需真實 LINE 媒體）",
    )
    parser.add_argument("--location", action="store_true", help="送出位置事件（測試佔位文字轉換）")
    parser.add_argument(
        "--image-message-id",
        default="",
        help="送出圖片事件；需提供真實 LINE image messageId 才能讓 router 下載成功",
    )
    return parser.parse_args()


def _build_event_test(args: argparse.Namespace) -> tuple[str, str, str] | None:
    """Build the (body, room_id, label) for --event follow|join.

    Args:
        args: Parsed CLI arguments.

    Returns:
        (body, room_id, label) ready for run_test, or None if the id the
        chosen event needs (--user-id for follow, --group-id for join) is
        missing — the caller should stop without sending anything.
    """
    if args.event == "follow":
        if not args.user_id:
            print("[ERROR] --event follow 需要 --user-id")
            return None
        return (
            make_follow_event(args.user_id),
            args.user_id,
            f"Follow 事件測試（userId={args.user_id}）",
        )
    if not args.group_id:
        print("[ERROR] --event join 需要 --group-id")
        return None
    return (
        make_join_event(args.group_id),
        args.group_id,
        f"Join 事件測試（groupId={args.group_id}）",
    )


def main() -> None:
    """Entry point."""
    args = build_args()

    if args.list_containers:
        list_hermes_containers()
        return

    env = load_env(ENV_FILE)
    secret = env.get("LINE_CHANNEL_SECRET", "")
    if not secret:
        print("[ERROR] .env 中找不到 LINE_CHANNEL_SECRET")
        return

    if args.ping:
        body = make_verification_ping()
        status, text = send_webhook(body, secret, timeout=args.send_timeout)
        print(f"Ping → {status} {text}")
        return

    if args.event:
        event_case = _build_event_test(args)
        if event_case is None:
            return
        body, room_id, label = event_case
    elif args.group_id:
        body = make_group_message(
            args.group_id, args.text, sender_id=args.sender_id, mention=args.mention
        )
        room_id = args.group_id
        label = f"Group 訊息測試（groupId={args.group_id}"
        if args.sender_id:
            label += f", sender={args.sender_id}"
        if args.mention:
            label += ", mention"
        label += "）"
    elif args.sticker:
        body = make_sticker_message(args.user_id)
        room_id = args.user_id
        label = f"貼圖訊息測試（userId={args.user_id}）"
    elif args.location:
        body = make_location_message(args.user_id)
        room_id = args.user_id
        label = f"位置訊息測試（userId={args.user_id}）"
    elif args.image_message_id:
        body = make_image_message(args.user_id, args.image_message_id)
        room_id = args.user_id
        label = f"圖片訊息測試（userId={args.user_id}, messageId={args.image_message_id}）"
    else:
        body = make_user_message(args.user_id, args.text)
        room_id = args.user_id
        label = f"User 訊息測試（userId={args.user_id}）"

    run_test(secret, body, room_id, label, wait=args.wait, send_timeout=args.send_timeout)


if __name__ == "__main__":
    main()
