"""改 repo 樣板 → 推到所有已存在房間 → 重啟容器生效（開發專用）。

這是**開發工具**，會**無條件覆蓋**所有房間自己 seed 出來的 mcp/plugins 副本與
config.yaml：不管房間副本有沒有被手改過，一律用 src/hermes/ 底下的樣板重寫
（房間各自的 mcp/<name>/.env 是唯一例外，那是房間自己的密鑰，會保留）。
config.yaml 也會用 config.template.yml 重新渲染，覆蓋房間對它的客製。
**production 千萬不要用**——正式環境房間的 mcp/plugins/config.yaml 是 write-once、
可被使用者自由編輯的，這支腳本會把它們全部踩掉。

範圍限制（本腳本管不到，要改得 rebuild image）
--------------------------------------------------
下面這些不是 seed 進房間 data/ 的東西，而是烤進 Hermes image 的，改了要重 build
`Dockerfile.hermes` 的 image、bump HERMES_IMAGE、重建房間容器，不在本腳本範圍：
- **skills**：房間 skills 由 Hermes gateway 開機自己做 manifest-based sync，不是這
  個 repo 或這支腳本管的（見 CLAUDE.md 的 Hermes Container Model）。
- **Node 依賴**：src/hermes/mcp/package.json → image 的 /opt/node_modules（所有 MCP
  共用；ESM walk-up 解析）。改依賴版本要重 build image。
- **Python 工具依賴**：src/hermes/runtime/pyproject.toml → image 的 /opt/tools/.venv
  （plugin script／skill 共用的第三方套件）。改依賴要重 build image。
本腳本只同步「房間各自一份、可編輯」的 mcp/plugins 原始碼與 config.yaml。

與 scripts/watch_restart.py 的分工
----------------------------------
兩支服務**不同的開發流**，不要混用：
- `watch_restart.py`：你**直接編輯某個房間自己的副本**（data/<room>/{mcp,plugins}/）
  時用它，存檔自動 restart 那一個房間。改的是房間副本，樣板不動。
- `dev_sync_src.py`（本腳本）：你**改 repo 樣板**（src/hermes/{mcp,plugin}/、
  config.template.yml）時用它，把樣板**推到所有已存在房間**、覆蓋它們的副本，再
  restart 所有 running 容器。改的是樣板，房間副本被樣板蓋掉。

使用方式
--------
    uv run python scripts/dev_sync_src.py                 # watch：監看樣板 + 每次變動全房間 sync + restart
    uv run python scripts/dev_sync_src.py --once          # 立刻 sync + restart 一次後退出
    uv run python scripts/dev_sync_src.py --room-id U_X    # 只處理指定房間
    uv run python scripts/dev_sync_src.py --no-restart     # 只 sync 不重啟
    uv run python scripts/dev_sync_src.py --interval 0.5   # 調輪詢間隔

按 Ctrl+C 停止。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from alice_office_router.config import Settings

ENV_FILE = Path(__file__).parent.parent / ".env"
# 與 watch_restart.py 相同（sibling 腳本各自獨立、無共用模組），改一邊記得改另一邊。
DEBOUNCE_SECONDS = 0.4  # let rapid multi-file saves (editor tmp+rename) settle

# 忽略清單與 container_manager._SEED_IGNORE 相同（複本；那份是 seed 用、這份是 sync
# 用）。刻意在這裡自己定義而不 import container_manager——import 它會連帶 import docker
# SDK，純 mcp/plugins sync 不該付這個成本（見模組 docstring 的範圍說明與 4. config
# 重渲染的 lazy import）。若忽略規則有變，兩處一起改。
_SEED_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".env", "node_modules", "package-lock.json"
)


def load_env(path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file, ignoring comments and blanks.

    Copy of watch_restart.load_env (sibling scripts share no module). Keep the
    two in sync if the parsing rules change.

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


def snapshot(paths: list[Path]) -> dict[str, float]:
    """Return a map of file path -> mtime for every file under the given paths.

    Copy of watch_restart.snapshot (sibling scripts share no module).

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


def _google_oauth_enabled(data_dir: Path) -> bool:
    """Whether this deployment has Google OAuth configured.

    Checks for the deployment-level Web-application client credentials seed
    (data/_google/gcp-oauth.keys.json) directly, deliberately NOT importing
    Settings.google_oauth_enabled — that keeps the pure sync path free of the
    container_manager/docker import. Corresponds to that property's file check.

    Args:
        data_dir: The resolved host data directory (holds the _google seed).

    Returns:
        True when the deployment-level Web-app credentials seed exists.
    """
    return (data_dir / "_google" / "gcp-oauth.keys.json").exists()


def _google_gated_names(mcp_templates_root: Path) -> frozenset[str]:
    """Names of MCP templates whose manifest sets requires_google_oauth: true.

    Copy of container_manager._google_gated_template_names' logic, kept here so
    the pure mcp/plugins sync path never imports container_manager (which pulls
    in the docker SDK). Keep the two in sync if the manifest key changes.

    Args:
        mcp_templates_root: src/hermes/mcp — one subdirectory per MCP template.

    Returns:
        Frozen set of gated template directory names (malformed manifests tolerated).
    """
    gated: set[str] = set()
    if not mcp_templates_root.is_dir():
        return frozenset(gated)
    for template_dir in mcp_templates_root.iterdir():
        manifest_path = template_dir / "mcp.manifest.yaml"
        if not template_dir.is_dir() or not manifest_path.exists():
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(manifest, dict) and manifest.get("requires_google_oauth"):
            gated.add(template_dir.name)
    return frozenset(gated)


def list_rooms(data_dir: Path, room_id: str | None) -> list[Path]:
    """Room data directories under data_dir (or just the one when room_id given).

    Skips names starting with '_' or '.': '_google' is the deployment-level
    Google seed source, not a room, and dotfiles (.DS_Store) aren't rooms either.

    Args:
        data_dir: The resolved host data directory.
        room_id: When given, restrict to just this one room.

    Returns:
        Existing room directories to process (empty if none match).
    """
    if room_id is not None:
        room_dir = data_dir / room_id
        return [room_dir] if room_dir.is_dir() else []
    if not data_dir.is_dir():
        return []
    return sorted(p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


def _clean_dest_dir(dest_dir: Path) -> None:
    """Remove everything under dest_dir except the room's own .env.

    The .env holds room-specific secrets (seeded once from .env.example, then
    possibly hand-edited) and must survive the clean sync — everything else is
    template-owned and gets replaced from source.

    Args:
        dest_dir: A room's seeded copy directory (e.g. data/<room>/mcp/<name>).
    """
    if not dest_dir.is_dir():
        return
    for entry in dest_dir.iterdir():
        if entry.name == ".env":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def clean_sync_template(src_dir: Path, dest_dir: Path) -> None:
    """Force one template dir onto its room copy: dest becomes src + kept .env.

    Clean sync: wipe dest (except its .env), then copy src in with _SEED_IGNORE
    applied. Guarantees the room copy equals the template and leaves no stale
    files — unconditional, the room's edits are discarded (dev-mode behavior).

    Args:
        src_dir: Repo template directory (src/hermes/{mcp,plugin}/<name>).
        dest_dir: Room's seeded copy directory to overwrite.
    """
    _clean_dest_dir(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dest_dir, ignore=_SEED_IGNORE, dirs_exist_ok=True)


def _skip_gated(
    name: str,
    dest_dir: Path,
    *,
    gated: frozenset[str],
    google_enabled: bool,
) -> bool:
    """Whether to skip a Google-gated template for this deployment/room.

    Mirrors _ensure_mcp_seed's gating: when Google OAuth is disabled, a gated
    template is added only if the room already has it (seeded while enabled) —
    a room without it is left without it (skip). Non-gated templates never skip.

    Args:
        name: Template directory name.
        dest_dir: Where this template would be synced in the room.
        gated: Names of Google-gated templates.
        google_enabled: Whether this deployment has Google OAuth configured.

    Returns:
        True to skip this template (don't add it), False to sync it.
    """
    if google_enabled or name not in gated:
        return False
    return not dest_dir.exists()


def _warn_orphans(templates_root: Path, dest_root: Path) -> None:
    """Log room copies with no matching template (left as-is, never deleted).

    Args:
        templates_root: Repo template group root (src/hermes/mcp or .../plugin).
        dest_root: Room's copy group root (data/<room>/mcp or .../plugins).
    """
    if not dest_root.is_dir():
        return
    names = (
        {p.name for p in templates_root.iterdir() if p.is_dir()}
        if templates_root.is_dir()
        else set()
    )
    for entry in sorted(dest_root.iterdir()):
        if entry.is_dir() and entry.name not in names:
            print(f"  留存（樣板已無此項，不刪）：{entry}", flush=True)


def _sync_template_group(
    templates_root: Path,
    dest_root: Path,
    *,
    gated: frozenset[str],
    google_enabled: bool,
) -> None:
    """Clean-sync every template under templates_root into dest_root.

    Args:
        templates_root: Repo template group root (src/hermes/mcp or .../plugin).
        dest_root: Room's copy group root (data/<room>/mcp or .../plugins).
        gated: Google-gated template names (empty for the plugin group).
        google_enabled: Whether this deployment has Google OAuth configured.
    """
    if not templates_root.is_dir():
        return
    for template_dir in sorted(templates_root.iterdir()):
        if not template_dir.is_dir():
            continue
        dest_dir = dest_root / template_dir.name
        if _skip_gated(template_dir.name, dest_dir, gated=gated, google_enabled=google_enabled):
            print(
                f"  跳過 Google-gated 樣板（未啟用且房間未 seed）：{template_dir.name}", flush=True
            )
            continue
        clean_sync_template(template_dir, dest_dir)
        print(f"  已同步 {dest_dir}", flush=True)
    _warn_orphans(templates_root, dest_root)


def force_sync_room(room_dir: Path, templates_dir: Path, *, google_enabled: bool) -> None:
    """Force every mcp/plugin template onto one room's seeded copies.

    data/<room>/mcp/<name>/ and data/<room>/plugins/<name>/ are each wiped
    (except their own .env) and rewritten from the repo template — the room's
    edits are discarded. Google-gated MCPs follow _skip_gated's rule.

    Args:
        room_dir: The room's data directory (data/<room>).
        templates_dir: Repo templates root (src/hermes).
        google_enabled: Whether this deployment has Google OAuth configured.
    """
    print(f"房間 {room_dir.name}：", flush=True)
    gated = _google_gated_names(templates_dir / "mcp")
    _sync_template_group(
        templates_dir / "mcp", room_dir / "mcp", gated=gated, google_enabled=google_enabled
    )
    _sync_template_group(
        templates_dir / "plugin",
        room_dir / "plugins",
        gated=frozenset(),
        google_enabled=google_enabled,
    )


def resync_config_yaml(room_dir: Path, data_dir: Path, templates_dir: Path) -> None:
    """Delete a room's config.yaml and re-render it from config.template.yml.

    Overwrites any hand-edits to the room's config.yaml — expected in dev mode.
    A no-op re-render (leaves no config.yaml) happens if LLM_BASE_URL/LLM_MODEL
    aren't set in .env; in host-dev they normally are.

    Args:
        room_dir: The room's data directory (data/<room>).
        data_dir: The resolved host data directory (Settings.DATA_DIR).
        templates_dir: Repo templates root (Settings.HERMES_TEMPLATES_DIR).
    """
    # Lazy import: only config re-render needs container_manager, whose import
    # pulls in the docker SDK. The pure mcp/plugins sync path must not pay that
    # cost — see module docstring's scope note.
    from alice_office_router.container_manager import _ensure_config_yaml

    (room_dir / "config.yaml").unlink(missing_ok=True)
    # ROUTER_IN_DOCKER=True only skips host-mode path validation: DATA_DIR and
    # HERMES_TEMPLATES_DIR are already the resolved absolute paths given here.
    settings = Settings(  # type: ignore[call-arg]
        DATA_DIR=data_dir,
        HERMES_TEMPLATES_DIR=templates_dir,
        ROUTER_IN_DOCKER=True,
    )
    _ensure_config_yaml(room_dir.name, settings)
    print(f"  重新渲染 config.yaml：{room_dir / 'config.yaml'}", flush=True)


def _restart_one(name: str) -> None:
    """docker restart one container, printing success/failure.

    Args:
        name: Container name to restart.
    """
    timestamp = time.strftime("%H:%M:%S")
    result = subprocess.run(["docker", "restart", name], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"[{timestamp}] docker restart {name} 完成 ✅", flush=True)
    else:
        print(f"[{timestamp}] docker restart {name} 失敗: {result.stderr.strip()}", flush=True)


def restart_running_containers() -> None:
    """Restart every running hermes_* container so it re-reads synced files.

    Stopped containers are left alone — they read the new files on next start.
    Uses the docker CLI (docker SDK is confined to container_manager.py by
    convention; scripts use the CLI — see watch_restart.py).
    """
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=hermes_", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    names = [n for n in result.stdout.splitlines() if n.strip()]
    if not names:
        print("沒有正在執行的 hermes_ 容器，跳過 restart。", flush=True)
        return
    for name in names:
        _restart_one(name)


def run_sync(
    data_dir: Path,
    templates_dir: Path,
    *,
    room_id: str | None,
    sync_templates: bool,
    sync_config: bool,
    restart: bool,
) -> None:
    """Force-sync targeted rooms per the given flags, then restart containers.

    Args:
        data_dir: The resolved host data directory.
        templates_dir: Repo templates root (src/hermes).
        room_id: When given, restrict to just this one room.
        sync_templates: Whether to clean-sync mcp/plugins copies.
        sync_config: Whether to re-render config.yaml.
        restart: Whether to restart running containers afterwards.
    """
    rooms = list_rooms(data_dir, room_id)
    if not rooms:
        print(f"[WARN] data 目錄 {data_dir} 底下沒有符合條件的房間。", flush=True)
        return
    google_enabled = _google_oauth_enabled(data_dir)
    for room_dir in rooms:
        if sync_templates:
            force_sync_room(room_dir, templates_dir, google_enabled=google_enabled)
        if sync_config:
            resync_config_yaml(room_dir, data_dir, templates_dir)
    _print_sync_summary(rooms, sync_templates=sync_templates, sync_config=sync_config)
    if restart:
        restart_running_containers()


def _print_sync_summary(rooms: list[Path], *, sync_templates: bool, sync_config: bool) -> None:
    """Print a one-line summary of what was synced.

    Args:
        rooms: Rooms that were processed.
        sync_templates: Whether mcp/plugins copies were synced.
        sync_config: Whether config.yaml was re-rendered.
    """
    actions = []
    if sync_templates:
        actions.append("mcp/plugins")
    if sync_config:
        actions.append("config.yaml")
    print(f"已同步 {len(rooms)} 個房間（{', '.join(actions)}）。", flush=True)


def _changed_keys(previous: dict[str, float], current: dict[str, float]) -> set[str]:
    """File paths that were added, removed, or had their mtime change.

    Args:
        previous: Prior snapshot (path -> mtime).
        current: Latest snapshot (path -> mtime).

    Returns:
        Set of path strings whose presence or mtime differs.
    """
    keys = set(previous) | set(current)
    return {k for k in keys if previous.get(k) != current.get(k)}


def _classify_changes(changed: set[str], templates_dir: Path) -> tuple[bool, bool]:
    """Map changed paths to (sync_templates, sync_config) flags.

    A change under mcp/ or plugin/ triggers a template force-sync; a change to
    config.template.yml triggers a config.yaml re-render. Both can fire.

    Args:
        changed: Paths that changed since the last snapshot.
        templates_dir: Repo templates root (src/hermes).

    Returns:
        (sync_templates, sync_config).
    """
    config_template = str(templates_dir / "config.template.yml")
    mcp_root = str(templates_dir / "mcp")
    plugin_root = str(templates_dir / "plugin")
    sync_config = config_template in changed
    sync_templates = any(c.startswith(mcp_root) or c.startswith(plugin_root) for c in changed)
    return sync_templates, sync_config


def resolve_watch_paths(templates_dir: Path) -> list[Path]:
    """The repo template sources to watch: mcp/, plugin/, config.template.yml.

    Args:
        templates_dir: Repo templates root (src/hermes).

    Returns:
        Existing paths to watch (missing ones skipped).
    """
    paths = [
        templates_dir / "mcp",
        templates_dir / "plugin",
        templates_dir / "config.template.yml",
    ]
    return [p for p in paths if p.exists()]


def watch(
    watch_paths: list[Path],
    data_dir: Path,
    templates_dir: Path,
    *,
    room_id: str | None,
    restart: bool,
    interval: float,
) -> None:
    """Poll watch_paths and run a targeted sync whenever files change.

    Args:
        watch_paths: Repo template sources to poll.
        data_dir: The resolved host data directory.
        templates_dir: Repo templates root (src/hermes).
        room_id: When given, restrict syncing to this one room.
        restart: Whether to restart running containers after each sync.
        interval: Seconds between polls.
    """
    previous = snapshot(watch_paths)
    try:
        while True:
            time.sleep(interval)
            current = snapshot(watch_paths)
            if current == previous:
                continue
            time.sleep(DEBOUNCE_SECONDS)
            current = snapshot(watch_paths)
            sync_templates, sync_config = _classify_changes(
                _changed_keys(previous, current), templates_dir
            )
            run_sync(
                data_dir,
                templates_dir,
                room_id=room_id,
                sync_templates=sync_templates,
                sync_config=sync_config,
                restart=restart,
            )
            previous = current
    except KeyboardInterrupt:
        print("\n已停止監看。")


def build_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="改 repo 樣板 → 推到所有已存在房間 → 重啟容器（開發專用，會覆蓋房間副本）"
    )
    parser.add_argument(
        "--once", action="store_true", help="立刻 sync + restart 一次後退出（不 watch）"
    )
    parser.add_argument("--room-id", default=None, help="只處理指定房間 room_id（預設全部房間）")
    parser.add_argument("--no-restart", action="store_true", help="只 sync 不 restart 容器")
    parser.add_argument("--interval", type=float, default=1.0, help="輪詢間隔秒數（預設 1 秒）")
    return parser.parse_args()


def _print_banner(
    templates_dir: Path, data_dir: Path, watch_paths: list[Path], room_id: str | None
) -> None:
    """Print the watch-mode startup banner.

    Args:
        templates_dir: Repo templates root being watched.
        data_dir: The resolved host data directory.
        watch_paths: Paths being watched.
        room_id: Target room, or None for all rooms.
    """
    print(f"監看樣板來源: {templates_dir}", flush=True)
    for p in watch_paths:
        print(f"  - {p}")
    print(f"data 目錄: {data_dir}")
    print(f"目標房間: {room_id or '全部'}")
    print("按 Ctrl+C 停止。\n", flush=True)


def main() -> None:
    """Entry point."""
    args = build_args()
    env = load_env(ENV_FILE)
    repo_root = Path(__file__).parent.parent
    data_dir = Path(env.get("HOST_DATA_DIR") or (repo_root / "data"))
    templates_dir = repo_root / "src" / "hermes"
    restart = not args.no_restart

    if args.once:
        run_sync(
            data_dir,
            templates_dir,
            room_id=args.room_id,
            sync_templates=True,
            sync_config=True,
            restart=restart,
        )
        return

    watch_paths = resolve_watch_paths(templates_dir)
    if not watch_paths:
        print(
            f"[ERROR] 找不到樣板來源目錄（{templates_dir}），確認在 repo 根目錄執行。", flush=True
        )
        return
    _print_banner(templates_dir, data_dir, watch_paths, args.room_id)
    watch(
        watch_paths,
        data_dir,
        templates_dir,
        room_id=args.room_id,
        restart=restart,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()
