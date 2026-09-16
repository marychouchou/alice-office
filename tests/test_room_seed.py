from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from alice_office_router.config import Settings
from alice_office_router.room_seed import (
    ensure_google_seed,
    ensure_mcp_seed,
    ensure_plugin_seed,
    ensure_soul_seed,
)

# Also defined in tests/test_container_manager.py (2nd occurrence — Rule of
# Three not yet hit; a 3rd caller should move these to tests/conftest.py).


def _write_mcp_template(templates_dir: Path, name: str, manifest_yaml: str) -> Path:
    """Write a minimal MCP template (manifest + placeholder server.mjs).

    Args:
        templates_dir: The HERMES_TEMPLATES_DIR root to write under.
        name: The MCP's directory name (e.g. "secretary").
        manifest_yaml: Raw mcp.manifest.yaml content for this template.

    Returns:
        Path to the created template directory (templates_dir/mcp/<name>/).
    """
    mcp_dir = templates_dir / "mcp" / name
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "server.mjs").write_text("// placeholder\n", encoding="utf-8")
    (mcp_dir / "mcp.manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
    return mcp_dir


def _settings_with_google(tmp_path: Path, *, enabled: bool) -> Settings:
    """Build Settings rooted at tmp_path, optionally with Google OAuth "enabled".

    "Enabled" here means google_oauth_enabled is True: a public URL is set
    and a fake web credentials file exists under google_web_creds_path.

    Args:
        tmp_path: Pytest tmp_path fixture.
        enabled: Whether to configure Google OAuth as enabled.

    Returns:
        A Settings instance for use in these tests.
    """
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path / "data",
        HOST_DATA_DIR=tmp_path / "data",
        HERMES_TEMPLATES_DIR=tmp_path / "templates",
        HERMES_API_SERVER_KEY="test_api_server_key",
        LLM_BASE_URL="https://spark2-vllm.dalue.co/v1",
        LLM_MODEL="qwen3-next",
        GOOGLE_OAUTH_PUBLIC_URL="https://router.example.com" if enabled else "",
    )
    if enabled:
        settings.google_web_creds_path.parent.mkdir(parents=True, exist_ok=True)
        settings.google_web_creds_path.write_text(
            '{"web": {"client_id": "x", "client_secret": "y"}}', encoding="utf-8"
        )
    return settings


def test_ensure_mcp_seed_copies_template_and_seeds_dotenv(tmp_path: Path) -> None:
    """A template's source + .env.example are copied into the room's own mcp/ dir."""
    templates_dir = tmp_path / "templates"
    mcp_dir = _write_mcp_template(templates_dir, "secretary", "command: node\nargs: [server.mjs]\n")
    (mcp_dir / ".env.example").write_text("GOOGLE_MAPS_API_KEY=\n", encoding="utf-8")
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path / "data",
        HOST_DATA_DIR=tmp_path / "data",
        HERMES_TEMPLATES_DIR=templates_dir,
        HERMES_API_SERVER_KEY="test_api_server_key",
    )

    ensure_mcp_seed("room_AAA", settings)

    seeded = settings.DATA_DIR / "room_AAA" / "mcp" / "secretary"
    assert (seeded / "server.mjs").exists()
    assert (seeded / "mcp.manifest.yaml").exists()
    assert (seeded / ".env").read_text(encoding="utf-8") == "GOOGLE_MAPS_API_KEY=\n"


def test_ensure_mcp_seed_does_not_overwrite_existing(tmp_path: Path) -> None:
    """Write-once: a room's own edits to its seeded MCP survive a second seed call."""
    templates_dir = tmp_path / "templates"
    _write_mcp_template(templates_dir, "secretary", "command: node\nargs: [server.mjs]\n")
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path / "data",
        HOST_DATA_DIR=tmp_path / "data",
        HERMES_TEMPLATES_DIR=templates_dir,
        HERMES_API_SERVER_KEY="test_api_server_key",
    )
    ensure_mcp_seed("room_AAA", settings)
    seeded_file = settings.DATA_DIR / "room_AAA" / "mcp" / "secretary" / "server.mjs"
    seeded_file.write_text("// room edit\n", encoding="utf-8")

    ensure_mcp_seed("room_AAA", settings)

    assert seeded_file.read_text(encoding="utf-8") == "// room edit\n"


def test_ensure_plugin_seed_copies_template(tmp_path: Path) -> None:
    """A plugin template's source is copied into the room's own plugins/ dir."""
    templates_dir = tmp_path / "templates"
    plugin_dir = templates_dir / "plugin" / "local-tools"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "tools.py").write_text("# placeholder\n", encoding="utf-8")
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path / "data",
        HOST_DATA_DIR=tmp_path / "data",
        HERMES_TEMPLATES_DIR=templates_dir,
        HERMES_API_SERVER_KEY="test_api_server_key",
    )

    ensure_plugin_seed("room_AAA", settings)

    assert (settings.DATA_DIR / "room_AAA" / "plugins" / "local-tools" / "tools.py").exists()


def test_google_gated_templates_skipped_when_disabled(tmp_path: Path) -> None:
    """A template with requires_google_oauth: true is not seeded when Google OAuth is disabled."""
    templates_dir = tmp_path / "templates"
    _write_mcp_template(
        templates_dir,
        "gmail",
        "command: /opt/tools/.venv/bin/python3\nargs: [server.py]\nrequires_google_oauth: true\n",
    )
    _write_mcp_template(templates_dir, "secretary", "command: node\nargs: [server.mjs]\n")
    settings = _settings_with_google(tmp_path, enabled=False)

    ensure_mcp_seed("room_AAA", settings)

    seeded_root = settings.DATA_DIR / "room_AAA" / "mcp"
    assert not (seeded_root / "gmail").exists()
    assert (seeded_root / "secretary").exists()


def test_google_gated_templates_seeded_when_enabled(tmp_path: Path) -> None:
    """A template with requires_google_oauth: true IS seeded when Google OAuth is enabled."""
    templates_dir = tmp_path / "templates"
    _write_mcp_template(
        templates_dir,
        "gmail",
        "command: /opt/tools/.venv/bin/python3\nargs: [server.py]\nrequires_google_oauth: true\n",
    )
    settings = _settings_with_google(tmp_path, enabled=True)

    ensure_mcp_seed("room_AAA", settings)

    assert (settings.DATA_DIR / "room_AAA" / "mcp" / "gmail").exists()


def test_ensure_google_seed_copies_deployment_creds_into_room_dir(tmp_path: Path) -> None:
    """ensure_google_seed copies both credential files into this room's own directory, once."""
    settings = _settings_with_google(tmp_path, enabled=True)
    settings.google_installed_creds_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_installed_creds_path.write_text(
        '{"installed": {"client_id": "x", "client_secret": "y"}}', encoding="utf-8"
    )

    ensure_google_seed("room_AAA", settings)

    room_dir = settings.room_google_dir("room_AAA")
    assert (room_dir / "gcp-oauth.keys.json").read_text(encoding="utf-8") == (
        settings.google_web_creds_path.read_text(encoding="utf-8")
    )
    assert (room_dir / "gcp-oauth.keys.installed.json").exists()


def test_ensure_google_seed_files_are_world_readable(tmp_path: Path) -> None:
    """Seeded credential files are 0644, not mkstemp's default 0600.

    Room container MCP subprocesses (google-calendar/gmail/drive) run as
    uid 10000, not root, and must be able to read these files. Source files
    are made 0600 here so the assertion proves the mode is set explicitly by
    _copy_atomically rather than merely inherited from the source.
    """
    settings = _settings_with_google(tmp_path, enabled=True)
    settings.google_installed_creds_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_installed_creds_path.write_text(
        '{"installed": {"client_id": "x", "client_secret": "y"}}', encoding="utf-8"
    )
    settings.google_web_creds_path.chmod(0o600)
    settings.google_installed_creds_path.chmod(0o600)

    ensure_google_seed("room_AAA", settings)

    room_dir = settings.room_google_dir("room_AAA")
    for filename in ("gcp-oauth.keys.json", "gcp-oauth.keys.installed.json"):
        dest = room_dir / filename
        assert stat.S_IMODE(dest.stat().st_mode) == 0o644


def test_ensure_google_seed_does_not_overwrite_existing_room_copy(tmp_path: Path) -> None:
    """A room's own credential copy, once seeded, is never overwritten by a later call."""
    settings = _settings_with_google(tmp_path, enabled=True)
    room_creds = settings.room_google_web_creds_path("room_AAA")
    room_creds.parent.mkdir(parents=True, exist_ok=True)
    room_creds.write_text('{"web": {"client_id": "room-own-edit"}}', encoding="utf-8")

    ensure_google_seed("room_AAA", settings)

    assert "room-own-edit" in room_creds.read_text(encoding="utf-8")


def test_ensure_google_seed_noop_when_disabled(tmp_path: Path) -> None:
    """ensure_google_seed does nothing when this deployment has no Google OAuth configured."""
    settings = _settings_with_google(tmp_path, enabled=False)

    ensure_google_seed("room_AAA", settings)

    assert not settings.room_google_dir("room_AAA").exists()


# ---------------------------------------------------------------------------
# ensure_soul_seed — per-room agent persona (SOUL.md), write-once.
# ---------------------------------------------------------------------------


def _settings_for_soul(tmp_path: Path) -> Settings:
    """Build minimal Settings for ensure_soul_seed tests.

    Args:
        tmp_path: Pytest tmp_path fixture.

    Returns:
        A Settings instance rooted at tmp_path.
    """
    return Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path / "data",
        HOST_DATA_DIR=tmp_path / "data",
        HERMES_TEMPLATES_DIR=tmp_path / "templates",
        HERMES_API_SERVER_KEY="test_api_server_key",
    )


def test_ensure_soul_seed_copies_template_verbatim(tmp_path: Path) -> None:
    """The template is copied byte-for-byte — no str.format() substitution."""
    settings = _settings_for_soul(tmp_path)
    settings.HERMES_TEMPLATES_DIR.mkdir(parents=True)
    template_text = "# Alice\n\nHolds a literal {placeholder} untouched.\n"
    (settings.HERMES_TEMPLATES_DIR / "SOUL.md").write_text(template_text, encoding="utf-8")

    ensure_soul_seed("room_AAA", settings)

    seeded = settings.DATA_DIR / "room_AAA" / "SOUL.md"
    assert seeded.read_text(encoding="utf-8") == template_text


def test_ensure_soul_seed_does_not_overwrite_existing(tmp_path: Path) -> None:
    """Write-once: a room's own SOUL.md (hand-edited, or Hermes's own default) survives."""
    settings = _settings_for_soul(tmp_path)
    settings.HERMES_TEMPLATES_DIR.mkdir(parents=True)
    (settings.HERMES_TEMPLATES_DIR / "SOUL.md").write_text("# Repo default\n", encoding="utf-8")
    room_soul = settings.DATA_DIR / "room_AAA"
    room_soul.mkdir(parents=True)
    existing_text = "You are Hermes Agent, a helpful assistant.\n"
    (room_soul / "SOUL.md").write_text(existing_text, encoding="utf-8")

    ensure_soul_seed("room_AAA", settings)

    assert (room_soul / "SOUL.md").read_text(encoding="utf-8") == existing_text


def test_ensure_soul_seed_logs_and_skips_when_template_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing repo template logs an error but never blocks room creation."""
    settings = _settings_for_soul(tmp_path)

    with caplog.at_level(logging.ERROR):
        ensure_soul_seed("room_AAA", settings)

    assert not (settings.DATA_DIR / "room_AAA" / "SOUL.md").exists()
    assert "Missing SOUL.md template" in caplog.text


def test_ensure_soul_seed_is_idempotent(tmp_path: Path) -> None:
    """Calling ensure_soul_seed twice leaves the room's copy unchanged."""
    settings = _settings_for_soul(tmp_path)
    settings.HERMES_TEMPLATES_DIR.mkdir(parents=True)
    (settings.HERMES_TEMPLATES_DIR / "SOUL.md").write_text("# Alice\n", encoding="utf-8")

    ensure_soul_seed("room_AAA", settings)
    seeded = settings.DATA_DIR / "room_AAA" / "SOUL.md"
    seeded.write_text("# Alice (room edit)\n", encoding="utf-8")
    ensure_soul_seed("room_AAA", settings)

    assert seeded.read_text(encoding="utf-8") == "# Alice (room edit)\n"


def test_repo_soul_template_exists_and_states_persona() -> None:
    """Guard the repo's own src/hermes/SOUL.md against losing its core persona.

    Pins the two user-specified requirements (企業級個人助理; proactively
    hinting at capabilities without claiming to be limited to them) so an
    edit that quietly drops either fails loudly here.
    """
    template = Path(__file__).parent.parent / "src" / "hermes" / "SOUL.md"

    assert template.exists()
    text = template.read_text(encoding="utf-8")
    assert text.strip()
    assert "企業級" in text
    assert "個人助理" in text
    assert "能力不僅限於" in text
