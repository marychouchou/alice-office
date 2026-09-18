"""本機假 LINE Platform：把 router 送出的 LINE API 呼叫錄下來，不用開手機。

為什麼需要
----------
Router 送回覆是**另開一條連線打 LINE 的伺服器**（見 docs/testing-paths.md），
所以用 `scripts/test_webhook.py` 偽造 webhook 時，回覆那段會打到真的 LINE API
並被拒絕（假 replyToken），只能從 router log 猜它想說什麼。把 router 的
`LINE_API_BASE_URL` 指到這支 stub，回覆／推播／成員名稱查詢就全部落到本機，
一行一個 JSON 記到 stdout 與檔案，直接看得到 router 到底回了什麼。

怎麼用
------
    # 1. 起 stub（預設 8099 埠，log 預設寫到 data/_line_stub/requests.jsonl）
    uv run python scripts/line_stub.py
    uv run python scripts/line_stub.py --port 9000 --log /tmp/line.jsonl

    # 2. router 端設 LINE_API_BASE_URL 後重啟（host 模式範例）
    LINE_API_BASE_URL=http://localhost:8099 uv run fastapi dev --reload-dir src

    # 3. 送一則偽造 webhook
    uv run python scripts/test_webhook.py --text "今天天氣如何？"

    # 4. 看 router 回了什麼（stub 的終端機，或）
    tail -f data/_line_stub/requests.jsonl | jq .

⚠️ `LINE_API_BASE_URL` 只給本機測試用，正式部署一定要留空（＝真的 LINE）。

支援的端點
----------
- `POST /v2/bot/message/reply`、`/push`：回 LINE 官方格式的 `sentMessages`
  （SDK 會驗這個 response，回空物件會讓 SDK 拋 ValidationError）。
- `GET /v2/bot/profile/<userId>`、`/v2/bot/{group,room}/<id>/member/<userId>`：
  回固定假 profile，displayName 取 userId 末四碼（例：`成員-ab12`）。
- `POST /v2/bot/message/validate/*`、`/v2/bot/chat/loading/start`：回 200 空物件
  （LINE 官方也是空 body）。
- 其他路徑：一律 200 `{}` 並在 log 記 `"matched": false`，讓 SDK 永遠不會炸。
"""

from __future__ import annotations

import argparse
import json
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import BaseServer

DEFAULT_PORT = 8099
# data/ 整個目錄已在 .gitignore 裡（底線前綴＝非房間目錄，跟 _conversations/、
# _google/ 同一層的慣例），所以這個 log 永遠不會被 commit 進去。
DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "_line_stub" / "requests.jsonl"
# 假 profile 的顯示名稱取 userId 末幾碼，剛好夠分辨兩個不同的假使用者。
_ID_SUFFIX_LEN = 4

JsonObject = dict[str, object]
# (URL 上抓到的參數, 請求 body) -> 回應 body
RouteHandler = Callable[[dict[str, str], JsonObject], JsonObject]


def fake_display_name(user_id: str) -> str:
    """Build the stub's fake display name for a LINE user id.

    Args:
        user_id: The bare LINE userId taken from the request path.

    Returns:
        "成員-<末四碼>", or plain "成員" when the path carried no id.
    """
    suffix = user_id[-_ID_SUFFIX_LEN:]
    return f"成員-{suffix}" if suffix else "成員"


def _sent_messages(_params: dict[str, str], body: JsonObject) -> JsonObject:
    """Answer a reply/push call the way the LINE Platform does.

    Args:
        _params: Unused path parameters.
        body: The parsed request body (its `messages` array sets the count).

    Returns:
        A `sentMessages` payload with one entry per message sent — the SDK's
        response model requires 1-5 entries, so never an empty list.
    """
    messages = body.get("messages")
    count = len(messages) if isinstance(messages, list) else 1
    return {
        "sentMessages": [
            {"id": f"stub-message-{index}", "quoteToken": f"stub-quote-{index}"}
            for index in range(max(count, 1))
        ]
    }


def _profile(params: dict[str, str], _body: JsonObject) -> JsonObject:
    """Answer a profile / group-member-profile lookup with fixed fake data.

    Args:
        params: Path parameters; `user_id` names the member looked up.
        _body: Unused request body (these are GET calls).

    Returns:
        A profile payload in LINE's wire format.
    """
    user_id = params.get("user_id", "")
    return {
        "userId": user_id,
        "displayName": fake_display_name(user_id),
        "pictureUrl": "https://example.invalid/stub-profile.png",
    }


def _empty(_params: dict[str, str], _body: JsonObject) -> JsonObject:
    """Answer endpoints whose real response body is empty.

    Args:
        _params: Unused path parameters.
        _body: Unused request body.

    Returns:
        An empty payload.
    """
    return {}


# Route table (method, path pattern, handler) — the LINE endpoints the router
# actually calls, per channels/line/client.py and channels/line/profiles.py.
_ROUTES: tuple[tuple[str, re.Pattern[str], RouteHandler], ...] = (
    ("POST", re.compile(r"^/v2/bot/message/(reply|push|multicast|broadcast)$"), _sent_messages),
    ("POST", re.compile(r"^/v2/bot/message/validate/[^/]+$"), _empty),
    ("POST", re.compile(r"^/v2/bot/chat/loading/start$"), _empty),
    ("GET", re.compile(r"^/v2/bot/profile/(?P<user_id>[^/]+)$"), _profile),
    (
        "GET",
        re.compile(r"^/v2/bot/(group|room)/(?P<room_id>[^/]+)/member/(?P<user_id>[^/]+)$"),
        _profile,
    ),
)


def resolve_response(method: str, path: str, body: JsonObject) -> tuple[JsonObject, bool]:
    """Route one request to its canned response.

    Args:
        method: HTTP method of the incoming request.
        path: Request path, query string already stripped.
        body: Parsed JSON request body ({} for GETs and unparsable bodies).

    Returns:
        (response payload, whether a known LINE endpoint matched). An unknown
        path still gets an empty 200 payload so the SDK never raises.
    """
    for route_method, pattern, handler in _ROUTES:
        match = pattern.match(path)
        if route_method == method and match:
            return handler(match.groupdict(), body), True
    return {}, False


def log_request(record: JsonObject, log_path: Path) -> None:
    """Emit one request record as a JSON line to stdout and the log file.

    Args:
        record: The request/response record to write.
        log_path: File the line is appended to (parents created on demand).
    """
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def reply_texts(body: JsonObject) -> list[str]:
    """Pull the plain reply texts out of a reply/push body, for readability.

    Args:
        body: Parsed request body.

    Returns:
        The `text` of every text message in the body, in order; empty for
        requests that carry no messages.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    return [str(m["text"]) for m in messages if isinstance(m, dict) and "text" in m]


class LineStubHandler(BaseHTTPRequestHandler):
    """Answers the handful of LINE endpoints the router calls, and logs them."""

    # Content-Length is always set below, so keep-alive is safe and the SDK's
    # connection pool doesn't have to reconnect per call.
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: BaseServer,
        *,
        log_path: Path,
    ) -> None:
        """Bind this handler to a log file.

        Args:
            request: The accepted client socket (passed straight through).
            client_address: The peer address (passed straight through).
            server: The owning server (passed straight through).
            log_path: File every request record is appended to.
        """
        self.log_path = log_path
        # BaseHTTPRequestHandler serves the whole request inside __init__,
        # so log_path must already be set before this call.
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:  # http.server mandates this exact method name
        """Handle a GET (profile lookups)."""
        self._handle()

    def do_POST(self) -> None:  # http.server mandates this exact method name
        """Handle a POST (reply, push, loading animation, validate)."""
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        """Silence http.server's own stderr line — log_request is the log.

        Args:
            format: Unused printf-style format string from http.server.
            *args: Unused format arguments.
        """

    def _read_body(self) -> tuple[JsonObject, str]:
        """Read and parse the request body.

        Returns:
            (parsed JSON object, raw text). The object is {} when the body is
            empty or is not a JSON object; the raw text is kept for the log.
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}, raw
        return (parsed if isinstance(parsed, dict) else {}), raw

    def _handle(self) -> None:
        """Route, answer, and log one request."""
        path = self.path.split("?", 1)[0]
        body, raw = self._read_body()
        payload, matched = resolve_response(self.command, path, body)
        record: JsonObject = {
            "ts": datetime.now(UTC).isoformat(),
            "method": self.command,
            "path": path,
            "matched": matched,
            "body": body or raw,
            "texts": reply_texts(body),
            "response": payload,
        }
        if not matched:
            record["warning"] = "unknown LINE endpoint — answered 200 {} so the SDK won't raise"
        log_request(record, self.log_path)
        self._respond(payload)

    def _respond(self, payload: JsonObject) -> None:
        """Write a 200 JSON response.

        Args:
            payload: Body to serialize.
        """
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def make_server(port: int, log_path: Path) -> ThreadingHTTPServer:
    """Create the stub HTTP server (bound, not yet serving).

    Args:
        port: TCP port to bind on localhost; 0 picks a free one (tests).
        log_path: File every request record is appended to.

    Returns:
        A ThreadingHTTPServer whose handler logs to `log_path`.
    """

    def handler_factory(
        request: socket.socket, client_address: tuple[str, int], server: BaseServer
    ) -> LineStubHandler:
        return LineStubHandler(request, client_address, server, log_path=log_path)

    return ThreadingHTTPServer(("127.0.0.1", port), handler_factory)


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="本機假 LINE Platform（錄下 router 送出的呼叫）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="監聽埠（預設 8099）")
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"請求紀錄檔（一行一個 JSON，預設 {DEFAULT_LOG_PATH}）",
    )
    args = parser.parse_args()

    server = make_server(args.port, args.log)
    port = server.server_address[1]
    print(f"LINE stub listening on http://localhost:{port}  (log: {args.log})")
    print(f"router 端請設 LINE_API_BASE_URL=http://localhost:{port} 後重啟；Ctrl-C 結束。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
