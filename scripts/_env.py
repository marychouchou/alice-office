"""Shared `.env` loader for the standalone dev/debug scripts under scripts/.

scripts/ is deliberately not a Python package (each script is meant to be run
standalone via `uv run python scripts/<name>.py`, and existing tests load a
script by file path rather than importing it — see tests/test_dev_sync_src.py)
so this can't be a normal `from scripts.foo import bar` import. Every script
that needs it instead does:

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _env import load_env  # noqa: E402

The `sys.path.insert` makes `import _env` resolve the same way whether the
script is executed directly (Python already puts a directly-run script's own
directory on sys.path, so the insert is a harmless no-op there) or loaded by
a test via `importlib.util.spec_from_file_location` (which does NOT do that
insertion automatically — the explicit insert is what makes that path work).

Extracted here because `load_env` previously had independent copies in
watch_restart.py and dev_sync_src.py (each commented "sibling scripts share
no module, keep in sync") — adding a third copy for debug_room.py would have
crossed the Rule of Three (see CLAUDE.md Growth Discipline), so this module
is the shared home instead.
"""

from __future__ import annotations

from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file, ignoring comments and blanks.

    Args:
        path: Path to the .env file.

    Returns:
        Dictionary of env var names to values. Empty if the file doesn't exist.
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
