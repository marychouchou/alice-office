from __future__ import annotations

import importlib.util
from pathlib import Path

# scripts/ is not an importable package, so load the dev tool by file path
# (same pattern as tests/test_dev_sync_src.py).
_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "debug_room.py"
_spec = importlib.util.spec_from_file_location("debug_room", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
debug_room = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(debug_room)


def test_resolve_data_dir_uses_host_data_dir_when_set() -> None:
    """HOST_DATA_DIR from .env takes priority over the repo's own ./data."""
    env = {"HOST_DATA_DIR": "/srv/alice/data"}

    result = debug_room.resolve_data_dir(env)

    assert result == Path("/srv/alice/data")


def test_resolve_data_dir_falls_back_to_repo_data() -> None:
    """An empty env falls back to the repo's own data/ directory."""
    result = debug_room.resolve_data_dir({})

    assert result.name == "data"
    assert result.parent == Path(debug_room.__file__).parent.parent


def test_container_name_prefixes_room_id() -> None:
    """Container names are always hermes_<room_id>."""
    assert debug_room.container_name("U_LOCAL_TEST") == "hermes_U_LOCAL_TEST"


def test_tail_file_returns_placeholder_for_missing_file(tmp_path: Path) -> None:
    """A non-existent log file is reported, not raised as an error."""
    missing = tmp_path / "agent.log"

    result = debug_room.tail_file(missing, lines=10)

    assert result == "(檔案不存在)"


def test_tail_file_returns_placeholder_for_empty_file(tmp_path: Path) -> None:
    """An existing but empty log file gets its own placeholder."""
    empty = tmp_path / "errors.log"
    empty.write_text("", encoding="utf-8")

    result = debug_room.tail_file(empty, lines=10)

    assert result == "(空檔案)"


def test_tail_file_returns_last_n_lines(tmp_path: Path) -> None:
    """Only the trailing N lines are returned, oldest lines dropped."""
    path = tmp_path / "gateway.log"
    path.write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")

    result = debug_room.tail_file(path, lines=3)

    assert result == "line18\nline19\nline20"


def test_check_key_paths_reports_existing_and_missing(tmp_path: Path) -> None:
    """Each KEY_PATHS entry is reported by relative path, existing or not."""
    room_dir = tmp_path / "U_LOCAL_TEST"
    (room_dir / "mcp").mkdir(parents=True)
    (room_dir / "config.yaml").write_text("model: x", encoding="utf-8")

    result = debug_room.check_key_paths(room_dir)

    assert result == {
        "config.yaml": True,
        "mcp": True,
        "plugins": False,
        "google/tokens.json": False,
    }
