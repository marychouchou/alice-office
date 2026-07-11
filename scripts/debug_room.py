"""One-shot diagnostic snapshot for a single room — container + logs + files.

一鍵印出某個房間目前的健康狀態，取代每次 debug 都要手動下好幾個 docker/ls/cat
指令：容器活著沒、docker stdout 最後幾行、data/<room_id>/logs/ 底下每個 log
檔的最後幾行、關鍵設定檔存不存在。詳細的症狀 → 排查流程見
docs/troubleshooting.md；這支腳本只負責把該文件「Log 地圖」列出的來源一次印
出來，不做判斷。

使用方式
--------
    uv run python scripts/debug_room.py <room_id>
    uv run python scripts/debug_room.py U196d1445f7fe156eac44c02106f364ec
    uv run python scripts/debug_room.py U_LOCAL_TEST --lines 50

DATA_DIR 解析方式與 watch_restart.py 相同：讀 repo 根目錄的 .env，取
HOST_DATA_DIR；沒有就 fallback 成 repo 的 ./data。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_env  # noqa: E402

ENV_FILE = Path(__file__).parent.parent / ".env"

# data/<room_id>/logs/ 底下由 Hermes 自己寫出的已知檔名（見
# docs/troubleshooting.md 的「Log 地圖」）。gateway-shutdown-diag.log 只在容器
# 收過至少一次 SIGTERM 後才會出現，其餘幾個從第一次開機就存在。
KNOWN_LOG_FILES = (
    "agent.log",
    "gateway.log",
    "errors.log",
    "mcp-stderr.log",
    "container-boot.log",
    "gateway-exit-diag.log",
    "gateway-shutdown-diag.log",
)

# 房間初始化流程會 write-once 寫出的關鍵檔案/目錄（見 container_manager.py 的
# _ensure_config_yaml / _ensure_mcp_seed / _ensure_plugin_seed / ensure_google_seed）。
KEY_PATHS = ("config.yaml", "mcp", "plugins", "google/tokens.json")

DEFAULT_DOCKER_LOG_LINES = 20
DEFAULT_FILE_LOG_LINES = 10


def resolve_data_dir(env: dict[str, str]) -> Path:
    """Resolve the host data directory, mirroring watch_restart.py's rule.

    Args:
        env: Parsed .env key/value pairs.

    Returns:
        HOST_DATA_DIR from .env if set, otherwise the repo's own ./data.
    """
    repo_root = Path(__file__).parent.parent
    return Path(env.get("HOST_DATA_DIR") or (repo_root / "data"))


def container_name(room_id: str) -> str:
    """Return the expected Hermes container name for a room.

    Args:
        room_id: The LINE userId / groupId / roomId.

    Returns:
        Docker container name string.
    """
    return f"hermes_{room_id}"


def tail_file(path: Path, lines: int) -> str:
    """Return the last N lines of a text file, or a placeholder if missing.

    Args:
        path: File to read.
        lines: Maximum number of trailing lines to return.

    Returns:
        The tailed text, or "(檔案不存在)" if path doesn't exist.
    """
    if not path.exists():
        return "(檔案不存在)"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:]) if content else "(空檔案)"


def check_key_paths(room_dir: Path) -> dict[str, bool]:
    """Check existence of each room-init key path under a room's data dir.

    Args:
        room_dir: The room's data directory (data/<room_id>).

    Returns:
        Mapping of relative path string (from KEY_PATHS) to whether it exists.
    """
    return {rel: (room_dir / rel).exists() for rel in KEY_PATHS}


def get_container_status(name: str) -> str | None:
    """Look up a container's docker ps status line.

    Args:
        name: Docker container name.

    Returns:
        The `docker ps` status string (e.g. "Up 3 hours"), or None if no
        container with this name exists (running or stopped).
    """
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Status}}"],
        capture_output=True,
        text=True,
    )
    status = result.stdout.strip()
    return status or None


def get_docker_logs(name: str, lines: int) -> str:
    """Fetch the tail of a container's docker logs (stdout+stderr).

    Args:
        name: Docker container name.
        lines: Number of trailing lines to fetch.

    Returns:
        Combined stdout/stderr text, or a diagnostic message if the docker
        command itself failed (e.g. container doesn't exist).
    """
    result = subprocess.run(
        ["docker", "logs", "--tail", str(lines), name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"(docker logs 失敗: {result.stderr.strip()})"
    return (result.stdout + result.stderr).strip() or "(無輸出)"


def print_section(title: str) -> None:
    """Print a banner line for a diagnostic section.

    Args:
        title: Section title to display.
    """
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_container_status(room_id: str) -> None:
    """Print whether the room's container exists/running and its status line.

    Args:
        room_id: The room to inspect.
    """
    print_section("Container 狀態")
    name = container_name(room_id)
    status = get_container_status(name)
    if status is None:
        print(f"  {name}: 不存在 ❌")
    else:
        print(f"  {name}: {status}")


def print_docker_logs(room_id: str, lines: int) -> None:
    """Print the tail of the room's container's docker logs.

    Args:
        room_id: The room to inspect.
        lines: Number of trailing lines to fetch.
    """
    print_section(f"docker logs --tail {lines}")
    print(get_docker_logs(container_name(room_id), lines))


def print_room_logs(room_dir: Path, lines: int) -> None:
    """Print the tail of every known log file under a room's logs/ dir.

    Args:
        room_dir: The room's data directory (data/<room_id>).
        lines: Number of trailing lines per file.
    """
    print_section(f"data/{room_dir.name}/logs/（每個檔案最後 {lines} 行）")
    logs_dir = room_dir / "logs"
    for filename in KNOWN_LOG_FILES:
        print(f"\n--- {filename} ---")
        print(tail_file(logs_dir / filename, lines))


def print_key_files(room_dir: Path) -> None:
    """Print existence checks for the room's write-once init files.

    Args:
        room_dir: The room's data directory (data/<room_id>).
    """
    print_section("關鍵檔案存在性")
    for rel, exists in check_key_paths(room_dir).items():
        mark = "✅" if exists else "❌"
        print(f"  {rel}: {mark}")


def run_diagnostics(room_id: str, data_dir: Path, *, docker_lines: int, file_lines: int) -> None:
    """Print a full diagnostic snapshot for one room.

    Args:
        room_id: The room to diagnose.
        data_dir: Resolved host data directory (holds data/<room_id>).
        docker_lines: Trailing line count for `docker logs`.
        file_lines: Trailing line count for each per-room log file.
    """
    room_dir = data_dir / room_id
    print(f"房間: {room_id}")
    print(f"資料夾: {room_dir}{'' if room_dir.is_dir() else '（不存在 ❌）'}")
    print_container_status(room_id)
    print_docker_logs(room_id, docker_lines)
    print_room_logs(room_dir, file_lines)
    print_key_files(room_dir)


def build_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="印出單一房間的診斷快照（container 狀態 + docker logs + 各 log 檔 tail + 關鍵檔案存在性）"
    )
    parser.add_argument("room_id", help="要診斷的房間 room_id（對應 hermes_<room_id> 容器）")
    parser.add_argument(
        "--lines",
        type=int,
        default=None,
        help=f"每個來源要看的行數（預設 docker logs {DEFAULT_DOCKER_LOG_LINES} 行、"
        f"檔案 log {DEFAULT_FILE_LOG_LINES} 行；指定後兩者統一套用同一個值）",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point."""
    args = build_args()
    env = load_env(ENV_FILE)
    data_dir = resolve_data_dir(env)
    docker_lines = args.lines if args.lines is not None else DEFAULT_DOCKER_LOG_LINES
    file_lines = args.lines if args.lines is not None else DEFAULT_FILE_LOG_LINES
    run_diagnostics(args.room_id, data_dir, docker_lines=docker_lines, file_lines=file_lines)


if __name__ == "__main__":
    main()
