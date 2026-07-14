from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# scripts/ is not an importable package, so load the migration tool by file path
# (same pattern as tests/test_dev_sync_src.py).
_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "migrate_room_keys.py"
_spec = importlib.util.spec_from_file_location("migrate_room_keys", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
migrate_room_keys = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_room_keys)

_OLD_ID = "U196d1445f7fe156eac44c02106f364ec"
_NEW_ID = f"line_{_OLD_ID}"
_OLD_LC = _OLD_ID.lower()
_NEW_LC = _NEW_ID.lower()

_CONFIG_YAML = (
    "mcp_servers:\n"
    "  drive:\n"
    "    env:\n"
    f"      GOOGLE_ACCOUNT_MODE: {_OLD_LC}\n"
    "  secretary:\n"
    "    env:\n"
    f"      SECRETARY_LINE_USER_ID: {_OLD_ID}\n"
)


def _make_room(data_dir: Path, room_id: str, *, with_google: bool = True) -> Path:
    """Create a minimal seeded room tree under data_dir.

    Args:
        data_dir: The data/ root.
        room_id: Directory name for the room.
        with_google: Whether to also write a google/tokens.json keyed by the
            lowercased room id.

    Returns:
        The created room directory path.
    """
    room_dir = data_dir / room_id
    (room_dir).mkdir(parents=True)
    (room_dir / "config.yaml").write_text(_CONFIG_YAML, encoding="utf-8")
    # A runtime file that must NOT be rewritten (proves we only touch config.yaml).
    (room_dir / "state.db").write_bytes(b"\x00binary")
    if with_google:
        google = room_dir / "google"
        google.mkdir()
        (google / "tokens.json").write_text(
            json.dumps({room_id.lower(): {"access_token": "x", "refresh_token": "y"}}),
            encoding="utf-8",
        )
    return room_dir


@pytest.fixture(autouse=True)
def _no_real_docker(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the docker-rm step so tests never shell out to docker."""
    fake = MagicMock()
    monkeypatch.setattr(migrate_room_keys, "remove_old_container", fake)
    return fake


# ---------------------------------------------------------------------------
# dry-run: reports but changes nothing
# ---------------------------------------------------------------------------


def test_dry_run_changes_nothing(tmp_path: Path, _no_real_docker: MagicMock) -> None:
    _make_room(tmp_path, _OLD_ID)

    count = migrate_room_keys.run(tmp_path, apply=False)

    assert count == 1
    # Directory keeps its bare name; files untouched; docker not touched.
    assert (tmp_path / _OLD_ID).is_dir()
    assert not (tmp_path / _NEW_ID).exists()
    assert _OLD_ID in (tmp_path / _OLD_ID / "config.yaml").read_text(encoding="utf-8")
    tokens = json.loads((tmp_path / _OLD_ID / "google" / "tokens.json").read_text())
    assert _OLD_LC in tokens
    _no_real_docker.assert_not_called()


# ---------------------------------------------------------------------------
# apply: renames dir, rewrites config.yaml + tokens.json, removes old container
# ---------------------------------------------------------------------------


def test_apply_renames_and_rewrites(tmp_path: Path, _no_real_docker: MagicMock) -> None:
    _make_room(tmp_path, _OLD_ID)

    count = migrate_room_keys.run(tmp_path, apply=True)

    assert count == 1
    assert not (tmp_path / _OLD_ID).exists()
    new_dir = tmp_path / _NEW_ID
    assert new_dir.is_dir()

    config = (new_dir / "config.yaml").read_text(encoding="utf-8")
    assert f"GOOGLE_ACCOUNT_MODE: {_NEW_LC}" in config
    assert f"SECRETARY_LINE_USER_ID: {_NEW_ID}" in config
    # No stray un-prefixed id remains, and no double prefix crept in.
    assert f"GOOGLE_ACCOUNT_MODE: {_OLD_LC}" not in config
    assert "line_line_" not in config

    tokens = json.loads((new_dir / "google" / "tokens.json").read_text())
    assert _NEW_LC in tokens
    assert _OLD_LC not in tokens
    assert tokens[_NEW_LC]["access_token"] == "x"

    _no_real_docker.assert_called_once_with(f"hermes_{_OLD_ID}")


def test_apply_skips_non_matching_and_prefixed_dirs(
    tmp_path: Path, _no_real_docker: MagicMock
) -> None:
    _make_room(tmp_path, _OLD_ID)
    # Already-migrated room (idempotency) and unrelated legacy dirs.
    (tmp_path / _NEW_ID).mkdir()
    (tmp_path / "room_TEST").mkdir()
    (tmp_path / "_google").mkdir()
    (tmp_path / "1U196d1445f7fe156eac44c02106f364ec").mkdir()  # bad leading char

    count = migrate_room_keys.run(tmp_path, apply=True)

    assert count == 1  # only the one bare-id room
    assert (tmp_path / _NEW_ID).is_dir()
    assert (tmp_path / "room_TEST").is_dir()
    assert (tmp_path / "_google").is_dir()
    assert (tmp_path / "1U196d1445f7fe156eac44c02106f364ec").is_dir()


def test_apply_is_idempotent_on_second_run(tmp_path: Path, _no_real_docker: MagicMock) -> None:
    _make_room(tmp_path, _OLD_ID)

    first = migrate_room_keys.run(tmp_path, apply=True)
    config_after_first = (tmp_path / _NEW_ID / "config.yaml").read_text(encoding="utf-8")
    second = migrate_room_keys.run(tmp_path, apply=True)
    config_after_second = (tmp_path / _NEW_ID / "config.yaml").read_text(encoding="utf-8")

    assert first == 1
    assert second == 0  # the prefixed dir is skipped
    assert config_after_first == config_after_second
    assert "line_line_" not in config_after_second


def test_apply_room_without_google_still_migrates(
    tmp_path: Path, _no_real_docker: MagicMock
) -> None:
    _make_room(tmp_path, _OLD_ID, with_google=False)

    count = migrate_room_keys.run(tmp_path, apply=True)

    assert count == 1
    assert (tmp_path / _NEW_ID / "config.yaml").exists()
    assert not (tmp_path / _NEW_ID / "google").exists()


def test_prefix_ids_is_idempotent() -> None:
    once = migrate_room_keys._prefix_ids(_CONFIG_YAML, _OLD_ID, _NEW_ID)
    twice = migrate_room_keys._prefix_ids(once, _OLD_ID, _NEW_ID)
    assert once == twice
    assert "line_line_" not in once
