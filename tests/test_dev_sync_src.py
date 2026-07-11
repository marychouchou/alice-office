from __future__ import annotations

import importlib.util
from pathlib import Path

# scripts/ is not an importable package, so load the dev tool by file path.
_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "dev_sync_src.py"
_spec = importlib.util.spec_from_file_location("dev_sync_src", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
dev_sync_src = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dev_sync_src)


def _write(path: Path, content: str) -> None:
    """Write content to path, creating parent directories as needed.

    Args:
        path: File path to write.
        content: Text content to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_templates(root: Path) -> Path:
    """Create a minimal src/hermes-style template tree under root.

    Args:
        root: Base temporary directory.

    Returns:
        Path to the created templates root (holds mcp/ and plugin/).
    """
    templates = root / "src_hermes"
    _write(templates / "mcp" / "secretary" / "server.mjs", "v2")
    _write(templates / "mcp" / "secretary" / ".env.example", "KEY=example")
    _write(templates / "plugin" / "local-tools" / "tools.py", "print('v2')")
    return templates


def _add_gated_mcp(templates: Path, name: str) -> None:
    """Add a Google-gated MCP template (requires_google_oauth: true).

    Args:
        templates: Templates root created by _make_templates.
        name: MCP template directory name to create.
    """
    _write(templates / "mcp" / name / "server.py", "v2")
    _write(templates / "mcp" / name / "mcp.manifest.yaml", "requires_google_oauth: true\n")


def test_force_sync_overwrites_modified_room_copy(tmp_path: Path) -> None:
    """A room's hand-edited copy is unconditionally overwritten by the template."""
    # Arrange
    templates = _make_templates(tmp_path)
    room = tmp_path / "data" / "roomA"
    _write(room / "mcp" / "secretary" / "server.mjs", "v1-EDITED")

    # Act
    dev_sync_src.force_sync_room(room, templates, google_enabled=False)

    # Assert
    assert (room / "mcp" / "secretary" / "server.mjs").read_text() == "v2"
    assert (room / "plugins" / "local-tools" / "tools.py").read_text() == "print('v2')"


def test_force_sync_preserves_room_env(tmp_path: Path) -> None:
    """The room's own mcp/<name>/.env secrets survive a clean sync."""
    # Arrange
    templates = _make_templates(tmp_path)
    room = tmp_path / "data" / "roomA"
    _write(room / "mcp" / "secretary" / ".env", "SECRET=room-owned")

    # Act
    dev_sync_src.force_sync_room(room, templates, google_enabled=False)

    # Assert
    assert (room / "mcp" / "secretary" / ".env").read_text() == "SECRET=room-owned"
    assert (room / "mcp" / "secretary" / ".env.example").read_text() == "KEY=example"


def test_force_sync_deletes_stale_files(tmp_path: Path) -> None:
    """Files/dirs in the room copy with no template counterpart are removed."""
    # Arrange
    templates = _make_templates(tmp_path)
    room = tmp_path / "data" / "roomA"
    _write(room / "mcp" / "secretary" / "stale.mjs", "old")
    _write(room / "mcp" / "secretary" / "sub" / "deep_stale.mjs", "old")

    # Act
    dev_sync_src.force_sync_room(room, templates, google_enabled=False)

    # Assert
    assert not (room / "mcp" / "secretary" / "stale.mjs").exists()
    assert not (room / "mcp" / "secretary" / "sub").exists()
    assert (room / "mcp" / "secretary" / "server.mjs").read_text() == "v2"


def test_list_rooms_skips_underscore_and_dot(tmp_path: Path) -> None:
    """_google and dotfiles are not treated as rooms."""
    # Arrange
    data = tmp_path / "data"
    (data / "roomA").mkdir(parents=True)
    (data / "roomB").mkdir()
    (data / "_google").mkdir()
    (data / ".hidden").mkdir()
    _write(data / ".DS_Store", "x")

    # Act
    rooms = dev_sync_src.list_rooms(data, None)

    # Assert
    assert sorted(p.name for p in rooms) == ["roomA", "roomB"]


def test_list_rooms_single_room(tmp_path: Path) -> None:
    """--room-id restricts to one room (empty when it doesn't exist)."""
    # Arrange
    data = tmp_path / "data"
    (data / "roomA").mkdir(parents=True)

    # Act / Assert
    assert dev_sync_src.list_rooms(data, "roomA") == [data / "roomA"]
    assert dev_sync_src.list_rooms(data, "missing") == []


def test_google_gated_not_added_when_disabled_and_absent(tmp_path: Path) -> None:
    """A gated MCP the room lacks is not created while Google OAuth is disabled."""
    # Arrange
    templates = _make_templates(tmp_path)
    _add_gated_mcp(templates, "drive")
    room = tmp_path / "data" / "roomA"

    # Act
    dev_sync_src.force_sync_room(room, templates, google_enabled=False)

    # Assert
    assert (room / "mcp" / "secretary").exists()
    assert not (room / "mcp" / "drive").exists()


def test_google_gated_updated_when_present(tmp_path: Path) -> None:
    """A gated MCP the room already has is still overwritten while disabled."""
    # Arrange
    templates = _make_templates(tmp_path)
    _add_gated_mcp(templates, "drive")
    room = tmp_path / "data" / "roomA"
    _write(room / "mcp" / "drive" / "server.py", "v1-EDITED")

    # Act
    dev_sync_src.force_sync_room(room, templates, google_enabled=False)

    # Assert
    assert (room / "mcp" / "drive" / "server.py").read_text() == "v2"


def test_google_gated_added_when_enabled(tmp_path: Path) -> None:
    """A gated MCP is added when this deployment has Google OAuth enabled."""
    # Arrange
    templates = _make_templates(tmp_path)
    _add_gated_mcp(templates, "drive")
    room = tmp_path / "data" / "roomA"

    # Act
    dev_sync_src.force_sync_room(room, templates, google_enabled=True)

    # Assert
    assert (room / "mcp" / "drive" / "server.py").read_text() == "v2"
