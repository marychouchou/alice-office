from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# src/hermes/ is not part of the router package (it is seeded into each room's
# container), so load the plugin's handlers by file path — same approach as
# tests/test_dev_sync_src.py. tools.py has no relative imports, so this works
# without a package context.
_TOOLS_PATH = (
    Path(__file__).parent.parent / "src" / "hermes" / "plugin" / "local-tools" / "tools.py"
)
_spec = importlib.util.spec_from_file_location("local_tools_tools", _TOOLS_PATH)
assert _spec is not None and _spec.loader is not None
local_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(local_tools)


def _share(path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Call handle_share_file with HERMES_HOME pointed at a temp directory.

    Args:
        path: The file to share.
        home: Stand-in for the container's /opt/data.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The handler's parsed JSON result.
    """
    monkeypatch.setenv("HERMES_HOME", str(home))
    result: dict[str, object] = json.loads(local_tools.handle_share_file({"path": str(path)}))
    return result


def test_share_file_copies_into_the_outbox_and_returns_a_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: a copy under outbox/<token>/ plus an outbox:// link."""
    source = tmp_path / "summary.md"
    source.write_text("# 摘要\n", encoding="utf-8")
    home = tmp_path / "opt-data"

    result = _share(source, home, monkeypatch)

    link = str(result["link"])
    assert link.startswith("outbox://")
    token = link.removeprefix("outbox://")
    # 43 characters is what secrets.token_urlsafe(32) produces, which is what
    # the router's marker regex matches.
    assert len(token) == 43
    assert result["filename"] == "summary.md"
    assert result["bytes"] == source.stat().st_size
    copied = home / "outbox" / token / "summary.md"
    assert copied.read_text(encoding="utf-8") == "# 摘要\n"
    # A copy, not a move: the agent may still be working with the original.
    assert source.exists()


def test_share_file_accepts_a_source_outside_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hr tool writes its xlsx to /tmp, so /opt/data is not a requirement."""
    source = tmp_path / "elsewhere" / "payroll.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"PK\x03\x04")
    home = tmp_path / "opt-data"

    result = _share(source, home, monkeypatch)

    assert "error" not in result
    assert result["filename"] == "payroll.xlsx"


def test_share_file_reports_a_missing_path_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that isn't a regular file comes back as an error the agent can read."""
    home = tmp_path / "opt-data"

    result = _share(tmp_path / "nope.md", home, monkeypatch)

    assert "error" in result
    assert "link" not in result
    assert not (home / "outbox").exists()


def test_share_file_refuses_a_file_over_the_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversize is refused in the container too, not only at the router."""
    monkeypatch.setattr(local_tools, "_SHARE_MAX_BYTES", 8)
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 9)
    home = tmp_path / "opt-data"

    result = _share(source, home, monkeypatch)

    assert "error" in result
    assert not (home / "outbox").exists()


def test_share_file_requires_a_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty path argument is an error, not an empty outbox directory."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "opt-data"))

    result = json.loads(local_tools.handle_share_file({}))

    assert "error" in result


def test_share_file_sanitizes_the_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control characters never reach a filename the router puts in a header."""
    source = tmp_path / "re\nport.md"
    source.write_text("x", encoding="utf-8")
    home = tmp_path / "opt-data"

    result = _share(source, home, monkeypatch)

    assert result["filename"] == "re_port.md"
