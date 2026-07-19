"""One-command end-to-end smoke test of the full channel pipeline.

Codifies the manual Phase-4 verification (see docs/channel-interface-plan.md)
into a repeatable script: it starts its *own* disposable uvicorn instance of
`alice_office_router.main:app`, drives the first-party API channel through auth,
validation, and a real happy path (router -> core -> container_manager -> real
Hermes container -> real LLM -> reply), optionally fires a correctly-signed
synthetic LINE webhook, then removes every artifact it created.

前置條件
--------
- Docker daemon 可用、`HERMES_IMAGE` 已 build 好、`.env` 已設定成 host 模式
  （`ROUTER_IN_DOCKER=false`、`DATA_DIR`／`HERMES_TEMPLATES_DIR` 指向 repo）。
- 這支腳本**不會改動 `.env`**：它用進程環境變數覆蓋 `API_CHANNEL_TOKEN`（設成
  一個腳本自訂值）與 `GOOGLE_OAUTH_GATE=false`（讓拋棄式房間不被授權 gate 擋），
  這些覆蓋只作用在它自己 spawn 的 uvicorn 子進程上。

使用方式
--------
    uv run python scripts/e2e_smoke.py            # 只跑 API 通道
    uv run python scripts/e2e_smoke.py --line      # 另外送一則合法簽章的 LINE webhook
    uv run python scripts/e2e_smoke.py --keep       # 保留 container / data 供檢查
    uv run python scripts/e2e_smoke.py --port 8901  # 8899 被占用時換 port

退出碼 0＝全部檢查通過，1＝有任何一項失敗（詳見每一行 numbered PASS/FAIL）。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_env  # noqa: E402

# ---------------------------------------------------------------------------
# Constants — deletion targets are hardcoded here and NEVER derived from user
# input, so cleanup can only ever touch this script's own disposable rooms.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

DEFAULT_PORT = 8899
# Script-generated bearer for the API channel (overrides .env only in the child
# uvicorn's env; setting it is what mounts the API channel — see enabled_adapters).
API_TOKEN = "e2e-smoke"

API_ROOM_KEY = "api_e2e"
API_CONTAINER = f"hermes_{API_ROOM_KEY}"

# A synthetic LINE native id (U + 32 hex) — matches the line_<native id> shape,
# lands in its own throwaway room, and can't collide with any real room.
LINE_NATIVE_ID = "U" + "f" * 32
LINE_ROOM_KEY = f"line_{LINE_NATIVE_ID}"
LINE_CONTAINER = f"hermes_{LINE_ROOM_KEY}"

# Defense in depth: remove_data_dir refuses any path whose name isn't one of these.
_DELETABLE_ROOM_KEYS = frozenset({API_ROOM_KEY, LINE_ROOM_KEY})

SHORT_TIMEOUT = 10.0
# Happy path spins a real container (30-60s cold start) then calls the real LLM.
HAPPY_TIMEOUT = 180.0
LINE_TIMEOUT = 30.0
ROUTER_BOOT_TIMEOUT = 30.0
LINE_CONTAINER_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Pure helpers (covered by tests/test_e2e_smoke.py)
# ---------------------------------------------------------------------------


def resolve_data_dir(env: dict[str, str]) -> Path:
    """Resolve the router's own data directory (where rooms are seeded).

    Args:
        env: Parsed .env key/value pairs.

    Returns:
        DATA_DIR if set, else HOST_DATA_DIR, else the repo's own ./data —
        matching how the router process itself resolves config.DATA_DIR.
    """
    override = env.get("DATA_DIR") or env.get("HOST_DATA_DIR")
    return Path(override) if override else (REPO_ROOT / "data")


def happy_path_ok(status: int, payload: object) -> bool:
    """Decide whether the happy-path response proves the agent replied.

    Args:
        status: HTTP status code of the API-channel response.
        payload: The decoded JSON body (or raw text on non-JSON).

    Returns:
        True only when the status is 200 and ``replies`` is a non-empty list
        carrying at least one non-blank string.
    """
    if status != 200 or not isinstance(payload, dict):
        return False
    replies = payload.get("replies")
    if not isinstance(replies, list) or not replies:
        return False
    return any(isinstance(r, str) and r.strip() for r in replies)


def cleanup_container_names(include_line: bool) -> list[str]:
    """Return the exact container names this run may remove (constants only).

    Args:
        include_line: Whether the --line webhook was fired (adds its container).

    Returns:
        The API container, plus the LINE container when include_line is True.
    """
    names = [API_CONTAINER]
    if include_line:
        names.append(LINE_CONTAINER)
    return names


def cleanup_data_dirs(data_dir: Path, include_line: bool) -> list[Path]:
    """Return the exact room data dirs this run may remove (constants only).

    Args:
        data_dir: The resolved router data directory.
        include_line: Whether the --line webhook was fired (adds its room dir).

    Returns:
        data_dir/api_e2e, plus data_dir/line_<native id> when include_line.
    """
    dirs = [data_dir / API_ROOM_KEY]
    if include_line:
        dirs.append(data_dir / LINE_ROOM_KEY)
    return dirs


def sign(body: str, secret: str) -> str:
    """Compute the LINE HMAC-SHA256 signature for a webhook body.

    Args:
        body: Raw JSON string of the webhook body.
        secret: LINE Channel Secret.

    Returns:
        Base64-encoded signature string (the x-line-signature value).
    """
    digest = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_line_webhook_body(native_id: str, text: str) -> str:
    """Build a synthetic LINE webhook body for one *addressed* group message.

    The event is a group/room text message that @mentions the bot (a mentionee
    flagged ``isSelf``), so core takes the group *addressed* path and drives the
    full container/agent pipeline. Without the mention the message would be
    classified unaddressed and merely observed — never reaching
    get_or_create_container — which would make run_line_check pass while
    exercising nothing (see run_line_check). No ``source.userId`` is included,
    so speaker-name resolution falls back locally without hitting the LINE API.

    Args:
        native_id: The bare LINE id (becomes room_key line_<native_id>).
        text: The message text to send.

    Returns:
        A JSON string; the fake replyToken means outbound delivery will fail
        against the real LINE API (expected — see run_line_check).
    """
    payload = {
        "events": [
            {
                "type": "message",
                "webhookEventId": "e2e-smoke-line-event",
                "replyToken": "e2e-smoke-fake-reply-token",
                "source": {"type": "room", "roomId": native_id},
                "message": {
                    "type": "text",
                    "text": text,
                    "mention": {"mentionees": [{"index": 0, "length": 1, "isSelf": True}]},
                },
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def build_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list (defaults to sys.argv when None) — passed
            explicitly by tests.

    Returns:
        Parsed argument namespace (port, line, keep).
    """
    parser = argparse.ArgumentParser(
        description="One-command e2e smoke test of the channel pipeline"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"uvicorn port（預設 {DEFAULT_PORT}）"
    )
    parser.add_argument("--line", action="store_true", help="另外送一則合法簽章的 LINE webhook")
    parser.add_argument("--keep", action="store_true", help="保留建立的 container / data 供檢查")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# HTTP + Docker + process helpers (exercised only during a live run)
# ---------------------------------------------------------------------------


def port_in_use(port: int) -> bool:
    """Report whether a TCP port on localhost is already bound.

    Args:
        port: The port to probe.

    Returns:
        True if binding the port fails (something already listens there).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Poll until a TCP port accepts a connection or the timeout elapses.

    Args:
        host: Host to connect to.
        port: Port to connect to.
        timeout: Maximum seconds to keep retrying.

    Returns:
        True once a connection succeeds, False if it never does.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _log_path(port: int) -> Path:
    """Return the temp file the child uvicorn's stdout/stderr is captured to."""
    return Path(tempfile.gettempdir()) / f"e2e_smoke_router_{port}.log"


def start_router(port: int, log_path: Path) -> subprocess.Popen[bytes]:
    """Start a disposable uvicorn instance of the router as a subprocess.

    Injects the API-channel token and disables the Google OAuth gate via the
    child's environment only — the on-disk .env is never modified.

    Args:
        port: Port for uvicorn to bind on localhost.
        log_path: File to capture the child's combined stdout/stderr into.

    Returns:
        The started subprocess handle.
    """
    env = os.environ.copy()
    env["API_CHANNEL_TOKEN"] = API_TOKEN
    env["GOOGLE_OAUTH_GATE"] = "false"
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "alice_office_router.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    log_fh = log_path.open("wb")  # noqa: SIM115 — fd is inherited by the child; parent copy closed below
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=log_fh, stderr=subprocess.STDOUT)
    log_fh.close()
    return proc


def terminate_router(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the uvicorn subprocess, force-killing if it doesn't stop.

    Args:
        proc: The subprocess to stop.
    """
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print("  已終止 router 子進程")


def post_api(
    base_url: str, body: dict[str, str], token: str | None, timeout: float
) -> tuple[int, object]:
    """POST a JSON body to the API channel, optionally with a bearer token.

    Args:
        base_url: Router base URL.
        body: JSON body to send.
        token: Bearer token, or None to omit the Authorization header.
        timeout: Client read timeout in seconds.

    Returns:
        Tuple of (status code, decoded JSON body or raw text).
    """
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client() as client:
        resp = client.post(
            f"{base_url}/webhooks/api/messages", json=body, headers=headers, timeout=timeout
        )
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text


def post_line(base_url: str, body: str, secret: str, timeout: float) -> int:
    """POST a correctly-signed LINE webhook body to the router.

    Args:
        base_url: Router base URL.
        body: Raw JSON webhook body string.
        secret: LINE Channel Secret used to sign the body.
        timeout: Client read timeout in seconds.

    Returns:
        The HTTP status code (the router replies 200 immediately; the agent
        call runs in a background task).
    """
    headers = {"Content-Type": "application/json", "x-line-signature": sign(body, secret)}
    with httpx.Client() as client:
        resp = client.post(
            f"{base_url}/webhooks/line",
            content=body.encode("utf-8"),
            headers=headers,
            timeout=timeout,
        )
    return resp.status_code


def container_exists(name: str) -> bool:
    """Report whether a container with this exact name exists (any state).

    Args:
        name: Docker container name.

    Returns:
        True if `docker ps -a` lists exactly this name.
    """
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return name in result.stdout.split()


def wait_for_container(name: str, timeout: float) -> bool:
    """Poll until a container appears or the timeout elapses.

    Args:
        name: Docker container name to wait for.
        timeout: Maximum seconds to keep polling.

    Returns:
        True once the container exists, False if it never appears.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if container_exists(name):
            return True
        time.sleep(2)
    return False


def remove_container(name: str) -> None:
    """Force-remove one container by exact name, tolerating its absence.

    Args:
        name: Docker container name to remove.
    """
    result = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  已移除容器 {name}")
    else:
        print(f"  容器 {name} 無需移除（{result.stderr.strip() or '不存在'}）")


def remove_data_dir(path: Path) -> None:
    """Delete one throwaway room data dir, refusing anything not whitelisted.

    Args:
        path: Room data directory to delete — its name must be one of this
            script's own disposable room keys, or deletion is skipped.
    """
    if path.name not in _DELETABLE_ROOM_KEYS:
        print(f"  [跳過] 拒絕刪除非測試資料夾：{path}")
        return
    if path.is_dir():
        shutil.rmtree(path)
        print(f"  已刪除 {path}")
    else:
        print(f"  資料夾 {path} 不存在，略過")


def tail_log(path: Path, lines: int = 40) -> None:
    """Print the last N lines of the captured router log, if it exists.

    Args:
        path: Router log file path.
        lines: Number of trailing lines to print.
    """
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"--- router log tail ({path}) ---")
    for line in content[-lines:]:
        print(line)


# ---------------------------------------------------------------------------
# Checklist + check runners
# ---------------------------------------------------------------------------


class Checklist:
    """Numbered PASS/FAIL accumulator — its printed lines are the CLI output."""

    def __init__(self) -> None:
        """Initialize an empty checklist."""
        self.count = 0
        self.passed = 0

    def record(self, label: str, ok: bool) -> bool:
        """Print one numbered PASS/FAIL line and tally the result.

        Args:
            label: Human-readable description of the check.
            ok: Whether the check passed.

        Returns:
            The ``ok`` value, so callers can branch on it.
        """
        self.count += 1
        if ok:
            self.passed += 1
        print(f"[{self.count}] {'PASS' if ok else 'FAIL'}  {label}")
        return ok

    @property
    def all_ok(self) -> bool:
        """Whether every recorded check passed."""
        return self.count == self.passed


def _preview(payload: object) -> str:
    """Render a short one-line preview of the happy-path replies for the log."""
    if isinstance(payload, dict) and isinstance(payload.get("replies"), list):
        return " | ".join(str(r) for r in payload["replies"])[:200]
    return str(payload)[:200]


def run_api_auth_and_validation(base_url: str, checklist: Checklist) -> None:
    """Run checks a-d: the fast auth + body-validation cases (no container).

    Args:
        base_url: Router base URL.
        checklist: Accumulator to record results into.
    """
    valid = {"room_key": API_ROOM_KEY, "text": "ping"}
    cases = [
        ("POST /webhooks/api/messages 無 Authorization → 401", valid, None, 401),
        ("錯誤 bearer token → 401", valid, "wrong-token", 401),
        ("room_key '../etc' → 422", {"room_key": "../etc", "text": "hi"}, API_TOKEN, 422),
        ("空白 text → 422", {"room_key": API_ROOM_KEY, "text": "   "}, API_TOKEN, 422),
    ]
    for label, body, token, expected in cases:
        status, _ = post_api(base_url, body, token, SHORT_TIMEOUT)
        checklist.record(f"{label}（實得 {status}）", status == expected)


def run_happy_path(base_url: str, checklist: Checklist, data_dir: Path, log_path: Path) -> None:
    """Run checks e-f: the real container + real LLM round trip and its artifacts.

    Args:
        base_url: Router base URL.
        checklist: Accumulator to record results into.
        data_dir: Resolved router data directory (to verify seeding).
        log_path: Router log file, tailed on failure for diagnosis.
    """
    body = {"room_key": API_ROOM_KEY, "text": "回覆一個字：好"}
    print("\n[進行中] happy path：拉起真實容器 + 呼叫真實 LLM（首次建室約 30-60 秒 + LLM）...")
    status, payload = post_api(base_url, body, API_TOKEN, HAPPY_TIMEOUT)
    ok = checklist.record(
        f"happy path 正確 bearer → 200 且 replies 非空（實得 {status}）",
        happy_path_ok(status, payload),
    )
    if ok:
        print(f"    replies: {_preview(payload)}")
    else:
        tail_log(log_path)
    seeded = container_exists(API_CONTAINER) and (data_dir / API_ROOM_KEY / "config.yaml").exists()
    checklist.record(f"容器 {API_CONTAINER} 存在且 data/{API_ROOM_KEY} 已 seed", seeded)


def run_line_check(base_url: str, env: dict[str, str], checklist: Checklist) -> None:
    """Run the optional --line check: a correctly-signed LINE webhook -> 200.

    Args:
        base_url: Router base URL.
        env: Parsed .env (for LINE_CHANNEL_SECRET).
        checklist: Accumulator to record results into.
    """
    body = build_line_webhook_body(LINE_NATIVE_ID, "回覆一個字：好")
    status = post_line(base_url, body, env.get("LINE_CHANNEL_SECRET", ""), LINE_TIMEOUT)
    checklist.record(f"POST /webhooks/line 正確簽章 → 200（實得 {status}）", status == 200)
    print(
        "    注意：outbound LINE 送達無法驗證——本測試用假 replyToken，router 的 push\n"
        "    fallback 會對真實 LINE API 失敗並記錄 log（屬預期行為，不代表管線壞掉）。"
    )
    if wait_for_container(LINE_CONTAINER, LINE_CONTAINER_TIMEOUT):
        print(f"    背景任務已建立 {LINE_CONTAINER}（cleanup 會移除）。")
    else:
        print(f"    [警告] {LINE_CONTAINER} 尚未出現；cleanup 仍會嘗試移除。")


# ---------------------------------------------------------------------------
# Cleanup + entry point
# ---------------------------------------------------------------------------


def _print_retained(include_line: bool, data_dir: Path) -> None:
    """Print what --keep left behind for manual inspection."""
    print("  --keep：保留以下 artifact 供檢查")
    for name in cleanup_container_names(include_line):
        print(f"    container: {name}")
    for path in cleanup_data_dirs(data_dir, include_line):
        print(f"    data dir : {path}")


def cleanup(
    proc: subprocess.Popen[bytes], *, include_line: bool, data_dir: Path, keep: bool
) -> None:
    """Tear down the run: always stop uvicorn; remove artifacts unless --keep.

    Args:
        proc: The uvicorn subprocess to terminate.
        include_line: Whether the LINE artifacts were created.
        data_dir: Resolved router data directory.
        keep: When True, retain containers/data dirs for inspection.
    """
    print("\n" + "=" * 60 + "\n  Cleanup\n" + "=" * 60)
    terminate_router(proc)
    if keep:
        _print_retained(include_line, data_dir)
        return
    for name in cleanup_container_names(include_line):
        remove_container(name)
    for path in cleanup_data_dirs(data_dir, include_line):
        remove_data_dir(path)


def _summarize(checklist: Checklist) -> int:
    """Print the final summary line and return the process exit code."""
    verdict = "全部通過 ✅" if checklist.all_ok else "有失敗 ❌"
    print(f"\n總結：{checklist.passed}/{checklist.count} 項通過 —— {verdict}")
    return 0 if checklist.all_ok else 1


def main() -> int:
    """Entry point: start router, run checks, clean up, return an exit code.

    Returns:
        0 when every check passed, 1 otherwise (or on a startup failure).
    """
    args = build_args()
    env = load_env(ENV_FILE)
    data_dir = resolve_data_dir(env)
    base_url = f"http://127.0.0.1:{args.port}"
    if port_in_use(args.port):
        print(f"[ERROR] port {args.port} 已被占用；換個 --port 或先關掉占用它的 process。")
        return 1
    log_path = _log_path(args.port)
    print(f"router log → {log_path}")
    proc = start_router(args.port, log_path)
    checklist = Checklist()
    try:
        if not wait_for_port("127.0.0.1", args.port, ROUTER_BOOT_TIMEOUT):
            print(f"[ERROR] router 未能在 {ROUTER_BOOT_TIMEOUT:.0f}s 內就緒")
            tail_log(log_path)
            return 1
        run_api_auth_and_validation(base_url, checklist)
        run_happy_path(base_url, checklist, data_dir, log_path)
        if args.line:
            run_line_check(base_url, env, checklist)
    finally:
        cleanup(proc, include_line=args.line, data_dir=data_dir, keep=args.keep)
    return _summarize(checklist)


if __name__ == "__main__":
    raise SystemExit(main())
