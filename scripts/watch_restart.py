"""Watch a room's seeded mcp/plugins source for changes, auto-restart on save.

存檔後自動 `docker restart hermes_<room_id>`，取代手動輸入 restart 指令。

MCP／plugin 原始碼不再是共用的 repo-level bind mount：每個房間第一次建立
container 時，會各自從 src/hermes/{mcp,plugin}/ seed 出一份自己的、可自由編輯
的副本，放在 data/<room_id>/{mcp,plugins}/ 底下（見 container_manager.py 的
_ensure_mcp_seed / _ensure_plugin_seed）。所以要監看的是**這個房間自己的
seed 副本**，不是 repo 裡的樣板——改 repo 樣板不會影響已建立的房間（write-once
/ frozen），只有改房間自己 data/ 底下那份才會在 restart 後生效。

前置條件
--------
目標房間的容器必須已經存在（用 test_webhook.py 送一則真訊息建立過一次）：
    uv run python scripts/test_webhook.py --user-id U_LOCAL_TEST

使用方式
--------
    uv run python scripts/watch_restart.py
    uv run python scripts/watch_restart.py --room-id U_LOCAL_TEST
    uv run python scripts/watch_restart.py --interval 0.5

按 Ctrl+C 停止。
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent / ".env"
DEBOUNCE_SECONDS = 0.4  # let rapid multi-file saves (editor tmp+rename) settle


def load_env(path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file, ignoring comments and blanks.

    Args:
        path: Path to the .env file.

    Returns:
        Dictionary of env var names to values.
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def resolve_watch_paths(env: dict[str, str], room_id: str) -> list[Path]:
    """Determine the room's own seeded mcp/plugins directories to watch.

    Mirrors container_manager._ensure_mcp_seed / _ensure_plugin_seed's
    destination paths: data/<room_id>/mcp/ and data/<room_id>/plugins/.
    Neither exists until the room's container has been created at least
    once (that's what seeds them) — see this module's docstring.

    Args:
        env: Parsed .env key/value pairs.
        room_id: The room whose seeded source directories should be watched.

    Returns:
        List of existing paths to watch (missing paths are skipped).
    """
    repo_root = Path(__file__).parent.parent
    data_dir = Path(env.get("HOST_DATA_DIR") or (repo_root / "data")) / room_id
    paths = [data_dir / "mcp", data_dir / "plugins"]
    return [p for p in paths if p.exists()]


def snapshot(paths: list[Path]) -> dict[str, float]:
    """Return a map of file path -> mtime for every file under the given paths.

    Args:
        paths: Files or directories to scan (directories are scanned recursively).

    Returns:
        Dictionary mapping file path string to modification time.
    """
    result: dict[str, float] = {}
    for base in paths:
        files = [base] if base.is_file() else [f for f in base.rglob("*") if f.is_file()]
        for file in files:
            result[str(file)] = file.stat().st_mtime
    return result


def restart_container(room_id: str) -> None:
    """Restart the hermes container for a room and print the result.

    Args:
        room_id: The room whose container should be restarted.
    """
    name = f"hermes_{room_id}"
    timestamp = time.strftime("%H:%M:%S")
    result = subprocess.run(["docker", "restart", name], capture_output=True, text=True)
    # flush=True: the watcher runs for hours — without it, output redirected to
    # a file/pipe stays buffered and restarts appear to happen silently.
    if result.returncode == 0:
        print(f"[{timestamp}] 偵測到變動 → docker restart {name} 完成 ✅", flush=True)
    else:
        print(
            f"[{timestamp}] 偵測到變動 → docker restart {name} 失敗: {result.stderr.strip()}",
            flush=True,
        )


def watch(paths: list[Path], room_id: str, interval: float) -> None:
    """Poll the given paths for mtime changes and restart the container on change.

    Args:
        paths: Paths to watch (from resolve_watch_paths).
        room_id: The room whose container should be restarted on change.
        interval: Seconds between polls.
    """
    previous = snapshot(paths)
    try:
        while True:
            time.sleep(interval)
            current = snapshot(paths)
            if current == previous:
                continue
            time.sleep(DEBOUNCE_SECONDS)
            current = snapshot(paths)
            restart_container(room_id)
            previous = current
    except KeyboardInterrupt:
        print("\n已停止監看。")


def build_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="監看指定房間已 seed 的 mcp/plugins 原始碼，存檔自動 restart 該房間的 hermes 容器"
    )
    parser.add_argument(
        "--room-id",
        default="U_LOCAL_TEST",
        help="要監看的房間 room_id（對應 hermes_<room_id> 容器）",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="輪詢間隔秒數（預設 1 秒）")
    return parser.parse_args()


def main() -> None:
    """Entry point."""
    args = build_args()
    env = load_env(ENV_FILE)
    paths = resolve_watch_paths(env, args.room_id)

    if not paths:
        print(
            f"[ERROR] 找不到房間 {args.room_id} 已 seed 的 mcp/plugins 目錄。"
            f"確認房間的 container 至少建立過一次"
            f"（uv run python scripts/test_webhook.py --user-id {args.room_id}），"
            f"或確認 .env 的 HOST_DATA_DIR 是否設對。"
        )
        return

    print(f"監看房間: hermes_{args.room_id}")
    for p in paths:
        print(f"  - {p}")
    # flush also covers the buffered banner lines above when stdout is a pipe.
    print("按 Ctrl+C 停止。\n", flush=True)

    watch(paths, args.room_id, args.interval)


if __name__ == "__main__":
    main()
