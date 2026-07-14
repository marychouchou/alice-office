"""One-shot migration: bare LINE room ids → channel-prefixed `line_<id>` keys.

Phase 3 of docs/channel-interface-plan.md. Before Phase 3 a room's key was the
bare LINE id (`U…`/`C…`/`R…`); now every room key is `line_<native_id>` so keys
never collide across channels and there is no permanent "LINE has no prefix"
special case. This script brings an existing `data/` tree up to the new shape.

For each `data/<bare id>/` room it:
  1. renames the dir to `data/line_<bare id>/` (the container bind-mounts this,
     so the new container `hermes_line_<id>` sees the same data);
  2. `docker rm -f hermes_<bare id>` — the next inbound message recreates the
     container under its new `hermes_line_<id>` name (not-found is ignored);
  3. rewrites the router-baked substitutions in the room's `config.yaml`
     (`GOOGLE_ACCOUNT_MODE` = lowercased account_key, `SECRETARY_LINE_USER_ID`
     = the `{room_id}` state key) so it matches a freshly seeded prefixed room;
  4. renames the top-level key in `google/tokens.json` from the old lowercased
     account_key to `line_<lowercased id>`, so the Google gate still sees the
     room as authorized.

Deliberately NOT touched: runtime files Hermes writes itself (logs, *.db,
*.bak), and the seeded secretary docs (`cron-recipes.md`, `config-snippet.yaml`)
whose `U…` occurrences are LINE-platform send targets / examples, not room keys
— prefixing those would corrupt valid LINE ids. Only `config.yaml` carries
router-baked room-key substitutions.

Default is a DRY-RUN (prints planned actions, changes nothing). Pass `--apply`
to execute. Idempotent: dirs already `line_`-prefixed are skipped, and any dir
that isn't a bare LINE id is reported and left untouched.

    uv run python scripts/migrate_room_keys.py            # dry-run
    uv run python scripts/migrate_room_keys.py --apply    # execute

⚠️ Back up first: `cp -a data data.bak-<date>` (see channel-interface-plan.md
「風險與回退」).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_env  # noqa: E402

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).parent.parent / ".env"

# Must equal channels/line/events.py::_ROOM_KEY_PREFIX and LineAdapter.name+"_".
ROOM_KEY_PREFIX = "line_"

# A bare LINE native id: user (U), group (C), or room (R) + 32 lowercase hex.
_NATIVE_LINE_ID_RE = re.compile(r"^[UCR][0-9a-f]{32}$")

# The only router-baked seed file carrying {room_id}/{account_key} substitutions
# (see container_manager._ensure_config_yaml / _format_mcp_section).
_ROOM_CONFIG_FILENAME = "config.yaml"
# Per-room Google token store; the lowercased account_key is its top-level key.
_TOKENS_REL_PATH = Path("google") / "tokens.json"


def resolve_data_dir(env: dict[str, str]) -> Path:
    """Resolve the host data directory (mirrors debug_room.resolve_data_dir).

    Args:
        env: Parsed `.env` mapping.

    Returns:
        HOST_DATA_DIR from `.env` if set, otherwise the repo's own `./data`.
    """
    repo_root = Path(__file__).parent.parent
    return Path(env.get("HOST_DATA_DIR") or (repo_root / "data"))


def is_native_line_id(name: str) -> bool:
    """Return True when `name` is a bare LINE native id needing the prefix.

    Args:
        name: A directory name under `data/`.

    Returns:
        True for `[UCR]` + 32 lowercase-hex chars; False otherwise (already
        prefixed, `_google`, or any other legacy/unrelated directory).
    """
    return bool(_NATIVE_LINE_ID_RE.match(name))


def remove_old_container(name: str) -> None:
    """Run `docker rm -f <name>`, tolerating a missing container or docker CLI.

    Args:
        name: Old container name, e.g. `hermes_U1234…`.
    """
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        logger.warning("docker CLI not found; skipped removing container %s", name)
        return
    if result.returncode != 0 and "No such container" not in result.stderr:
        logger.warning(
            "docker rm -f %s exited %d: %s", name, result.returncode, result.stderr.strip()
        )


def _prefix_ids(text: str, old_id: str, new_id: str) -> str:
    """Prefix the raw id and its lowercased account_key, idempotently.

    Args:
        text: File contents to rewrite.
        old_id: Bare LINE id, e.g. `U1234…`.
        new_id: Prefixed key, e.g. `line_U1234…`.

    Returns:
        `text` with every standalone occurrence of the raw id and the
        lowercased id prefixed. A negative lookbehind leaves an id that is
        already `line_`-prefixed alone, so re-running never double-prefixes.
    """
    guard = re.escape(ROOM_KEY_PREFIX)
    for old, new in ((old_id, new_id), (old_id.lower(), new_id.lower())):
        text = re.sub(rf"(?<!{guard}){re.escape(old)}", new, text)
    return text


def _rewrite_room_config(room_dir: Path, old_id: str, new_id: str, *, apply: bool) -> list[str]:
    """Rewrite the baked room-key substitutions in the room's config.yaml.

    Args:
        room_dir: The room's data directory.
        old_id: Bare LINE id.
        new_id: Prefixed key.
        apply: When False, only report; when True, write the file back.

    Returns:
        A one-item action list if config.yaml changed, else an empty list.
    """
    config_path = room_dir / _ROOM_CONFIG_FILENAME
    if not config_path.exists():
        return []
    content = config_path.read_text(encoding="utf-8")
    rewritten = _prefix_ids(content, old_id, new_id)
    if rewritten == content:
        return []
    if apply:
        config_path.write_text(rewritten, encoding="utf-8")
    return [
        f"rewrite {_ROOM_CONFIG_FILENAME}: {old_id}|{old_id.lower()} → {new_id}|{new_id.lower()}"
    ]


def _rewrite_tokens(room_dir: Path, old_id: str, new_id: str, *, apply: bool) -> list[str]:
    """Rename the account_key key in the room's google/tokens.json.

    Args:
        room_dir: The room's data directory.
        old_id: Bare LINE id.
        new_id: Prefixed key.
        apply: When False, only report; when True, write the file back.

    Returns:
        A one-item action list if the key was renamed, else an empty list.
    """
    tokens_path = room_dir / _TOKENS_REL_PATH
    if not tokens_path.exists():
        return []
    old_key, new_key = old_id.lower(), new_id.lower()
    try:
        raw: object = json.loads(tokens_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not parse %s (%s); skipping token key rewrite", tokens_path, exc)
        return []
    if not isinstance(raw, dict) or old_key not in raw:
        return []
    renamed = {(new_key if key == old_key else key): value for key, value in raw.items()}
    if apply:
        tokens_path.write_text(json.dumps(renamed, indent=2), encoding="utf-8")
    return [f"rewrite {_TOKENS_REL_PATH}: key {old_key} → {new_key}"]


def migrate_room(data_dir: Path, old_id: str, *, apply: bool) -> list[str]:
    """Migrate one bare-id room to its `line_`-prefixed form.

    Args:
        data_dir: The `data/` root.
        old_id: The bare LINE id (also the current directory name).
        apply: When False, plan only; when True, perform the changes.

    Returns:
        The ordered list of action descriptions (planned or performed).
    """
    new_id = f"{ROOM_KEY_PREFIX}{old_id}"
    old_dir = data_dir / old_id
    actions = [f"rename dir: {old_id} → {new_id}"]
    if apply:
        old_dir.rename(data_dir / new_id)
    room_dir = data_dir / new_id if apply else old_dir

    container = f"hermes_{old_id}"
    actions.append(f"docker rm -f {container} (ignore if absent)")
    if apply:
        remove_old_container(container)

    actions.extend(_rewrite_room_config(room_dir, old_id, new_id, apply=apply))
    actions.extend(_rewrite_tokens(room_dir, old_id, new_id, apply=apply))
    return actions


def _classify(entry: Path) -> str:
    """Bucket a `data/` entry: `migrate`, `skip_prefixed`, or `skip_other`."""
    if not entry.is_dir():
        return "skip_other"
    if entry.name.startswith(ROOM_KEY_PREFIX):
        return "skip_prefixed"
    return "migrate" if is_native_line_id(entry.name) else "skip_other"


def run(data_dir: Path, *, apply: bool) -> int:
    """Scan `data_dir` and migrate (or plan to migrate) every bare-id room.

    Args:
        data_dir: The `data/` root to scan.
        apply: When False, dry-run; when True, execute.

    Returns:
        The number of rooms migrated (or that would be migrated in dry-run).
    """
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] scanning {data_dir}")
    if not data_dir.is_dir():
        print(f"  data dir not found: {data_dir}")
        return 0
    migrated = 0
    for entry in sorted(data_dir.iterdir()):
        bucket = _classify(entry)
        if bucket == "skip_prefixed":
            print(f"  skip (already prefixed): {entry.name}")
        elif bucket == "skip_other":
            if entry.is_dir():
                print(f"  skip (not a bare LINE id): {entry.name}")
        else:
            print(f"  migrate: {entry.name}")
            for action in migrate_room(data_dir, entry.name, apply=apply):
                print(f"      - {action}")
            migrated += 1
    verb = "migrated" if apply else "to migrate (re-run with --apply)"
    print(f"[{mode}] {migrated} room(s) {verb}")
    return migrated


def main() -> None:
    """Parse args and run the migration (dry-run unless --apply)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="execute changes (default: dry-run)")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="override data dir (default: HOST_DATA_DIR from .env, else repo ./data)",
    )
    args = parser.parse_args()
    data_dir = args.data_dir or resolve_data_dir(load_env(ENV_FILE))
    run(data_dir, apply=args.apply)


if __name__ == "__main__":
    main()
