from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import docker.errors
import httpx
import pytest

from alice_office_router.config import Settings
from alice_office_router.container_manager import (
    CONTAINER_GOOGLE_DIR,
    _build_volume_config,
    _ensure_config_yaml,
    _ensure_mcp_seed,
    _ensure_plugin_seed,
    _wait_until_ready,
    get_or_create_container,
)

SETTINGS_IN_DOCKER = Settings(
    LINE_CHANNEL_SECRET="test_secret",
    LINE_CHANNEL_ACCESS_TOKEN="test_token",
    DATA_DIR=Path("/tmp/test_data"),
    HOST_DATA_DIR=Path("/tmp/test_data"),
    HERMES_TEMPLATES_DIR=Path("/tmp/test_templates"),
    HERMES_INTERNAL_PORT=8642,
    HERMES_API_SERVER_KEY="test_api_server_key",
    ROUTER_IN_DOCKER=True,
    LLM_BASE_URL="https://spark2-vllm.dalue.co/v1",
    LLM_API_KEY="sk-test",
    LLM_MODEL="qwen3-next",
)

SETTINGS_ON_HOST = Settings(
    LINE_CHANNEL_SECRET="test_secret",
    LINE_CHANNEL_ACCESS_TOKEN="test_token",
    DATA_DIR=Path("/tmp/test_data"),
    HOST_DATA_DIR=Path("/tmp/test_data"),
    HERMES_TEMPLATES_DIR=Path("/tmp/test_templates"),
    HERMES_INTERNAL_PORT=8642,
    HERMES_API_SERVER_KEY="test_api_server_key",
    ROUTER_IN_DOCKER=False,
)

CONTAINER_NAME = "hermes_room_AAA"
EXPECTED_URL_DOCKER = f"http://{CONTAINER_NAME}:8642"
EXPECTED_URL_HOST = "http://localhost:54321"


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


def _write_config_template(templates_dir: Path) -> None:
    """Write the config.yaml.format() template used by _ensure_config_yaml.

    Mirrors src/hermes/config.template.yml so tests exercise the same
    placeholder shape without depending on the real repo file's content.

    Args:
        templates_dir: The HERMES_TEMPLATES_DIR root to write under.
    """
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "config.template.yml").write_text(
        "model:\n"
        "  default: {model}\n"
        "  provider: custom\n"
        "providers:\n"
        "  custom:\n"
        "    base_url: {base_url}\n"
        "    key_env: LLM_API_KEY\n"
        "    default_model: {model}\n"
        "    models:\n"
        "      - {model}\n"
        "{plugins_section}\n"
        "{mcp_section}\n",
        encoding="utf-8",
    )


def _make_running_container(host_port: str = "54321") -> MagicMock:
    """Return a mock container that is running and has a published port.

    Args:
        host_port: Simulated host port string in the Docker ports mapping.

    Returns:
        Configured mock Container object.
    """
    c = MagicMock()
    c.name = CONTAINER_NAME
    c.status = "running"
    c.ports = {"8642/tcp": [{"HostIp": "0.0.0.0", "HostPort": host_port}]}
    return c


def _make_mock_client(container: MagicMock) -> MagicMock:
    """Create a mock Docker client that returns the given container.

    Args:
        container: Mock container object to return from containers.get().

    Returns:
        Configured mock DockerClient.
    """
    client = MagicMock()
    client.containers.get.return_value = container
    return client


def test_running_container_returns_docker_url() -> None:
    """When running inside Docker, return container-name URL without recreating."""
    mock_container = _make_running_container()
    mock_client = _make_mock_client(mock_container)

    with (
        patch("alice_office_router.container_manager.docker.from_env", return_value=mock_client),
        patch("alice_office_router.container_manager._wait_until_ready") as mock_wait,
    ):
        url = get_or_create_container("room_AAA", SETTINGS_IN_DOCKER)

    assert url == EXPECTED_URL_DOCKER
    mock_container.start.assert_not_called()
    mock_wait.assert_not_called()


def test_running_container_returns_host_url() -> None:
    """When running on the host, return localhost URL with published port."""
    mock_container = _make_running_container(host_port="54321")
    mock_client = _make_mock_client(mock_container)

    with patch("alice_office_router.container_manager.docker.from_env", return_value=mock_client):
        url = get_or_create_container("room_AAA", SETTINGS_ON_HOST)

    assert url == EXPECTED_URL_HOST


def test_stopped_container_is_restarted() -> None:
    """When container exists but is stopped, it should be started before returning URL."""
    mock_container = _make_running_container()
    mock_container.status = "exited"
    mock_client = _make_mock_client(mock_container)

    with (
        patch("alice_office_router.container_manager.docker.from_env", return_value=mock_client),
        patch("alice_office_router.container_manager._wait_until_ready") as mock_wait,
    ):
        url = get_or_create_container("room_AAA", SETTINGS_IN_DOCKER)

    assert url == EXPECTED_URL_DOCKER
    mock_container.start.assert_called_once()
    mock_wait.assert_called_once_with(EXPECTED_URL_DOCKER)


def test_missing_container_is_created_with_hermes_env() -> None:
    """When container does not exist, create it with api_server + LLM env vars, no LINE creds."""
    mock_container = _make_running_container()
    mock_client = MagicMock()
    mock_client.containers.get.side_effect = docker.errors.NotFound("not found")
    mock_client.containers.run.return_value = mock_container

    with (
        patch("alice_office_router.container_manager.docker.from_env", return_value=mock_client),
        patch("alice_office_router.container_manager._wait_until_ready"),
        patch("alice_office_router.container_manager._ensure_data_dir"),
        patch("alice_office_router.container_manager._ensure_mcp_seed"),
        patch("alice_office_router.container_manager._ensure_plugin_seed"),
        patch("alice_office_router.container_manager._ensure_config_yaml"),
    ):
        url = get_or_create_container("room_AAA", SETTINGS_IN_DOCKER)

    assert url == EXPECTED_URL_DOCKER
    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["name"] == CONTAINER_NAME
    assert call_kwargs["detach"] is True
    assert call_kwargs["command"] == ["gateway", "run"]
    assert call_kwargs["volumes"] == {
        "/tmp/test_data/room_AAA": {"bind": "/opt/data", "mode": "rw"},
    }
    env = call_kwargs["environment"]
    assert env["API_SERVER_KEY"] == "test_api_server_key"
    assert env["API_SERVER_HOST"] == "0.0.0.0"
    assert env["LLM_API_KEY"] == "sk-test"
    assert "LINE_CHANNEL_ACCESS_TOKEN" not in env
    assert "LINE_CHANNEL_SECRET" not in env


def test_missing_container_publishes_port_on_host() -> None:
    """When ROUTER_IN_DOCKER=False, new container must publish port to host."""
    mock_container = _make_running_container(host_port="54321")
    mock_client = MagicMock()
    mock_client.containers.get.side_effect = docker.errors.NotFound("not found")
    mock_client.containers.run.return_value = mock_container

    with (
        patch("alice_office_router.container_manager.docker.from_env", return_value=mock_client),
        patch("alice_office_router.container_manager._wait_until_ready"),
        patch("alice_office_router.container_manager._ensure_data_dir"),
        patch("alice_office_router.container_manager._ensure_mcp_seed"),
        patch("alice_office_router.container_manager._ensure_plugin_seed"),
        patch("alice_office_router.container_manager._ensure_config_yaml"),
        patch("alice_office_router.container_manager._find_free_port", return_value=54321),
    ):
        url = get_or_create_container("room_AAA", SETTINGS_ON_HOST)

    assert url == EXPECTED_URL_HOST
    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["ports"] == {"8642/tcp": 54321}


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

    _ensure_mcp_seed("room_AAA", settings)

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
    _ensure_mcp_seed("room_AAA", settings)
    seeded_file = settings.DATA_DIR / "room_AAA" / "mcp" / "secretary" / "server.mjs"
    seeded_file.write_text("// room edit\n", encoding="utf-8")

    _ensure_mcp_seed("room_AAA", settings)

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

    _ensure_plugin_seed("room_AAA", settings)

    assert (settings.DATA_DIR / "room_AAA" / "plugins" / "local-tools" / "tools.py").exists()


def test_ensure_config_yaml_writes_provider_block(tmp_path: Path) -> None:
    """When no config.yaml exists yet, one is written with the shared LLM provider."""
    templates_dir = tmp_path / "templates"
    _write_config_template(templates_dir)
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path,
        HOST_DATA_DIR=tmp_path,
        HERMES_TEMPLATES_DIR=templates_dir,
        HERMES_API_SERVER_KEY="test_api_server_key",
        LLM_BASE_URL="https://spark2-vllm.dalue.co/v1",
        LLM_MODEL="qwen3-next",
    )
    (tmp_path / "room_AAA").mkdir()

    _ensure_config_yaml("room_AAA", settings)

    written = (tmp_path / "room_AAA" / "config.yaml").read_text(encoding="utf-8")
    assert "base_url: https://spark2-vllm.dalue.co/v1" in written
    assert "default: qwen3-next" in written
    assert "key_env: LLM_API_KEY" in written


def test_ensure_config_yaml_skips_when_template_missing(tmp_path: Path) -> None:
    """No config.yaml is written if config.template.yml doesn't exist under HERMES_TEMPLATES_DIR."""
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path,
        HOST_DATA_DIR=tmp_path,
        HERMES_TEMPLATES_DIR=tmp_path / "nonexistent_templates",
        HERMES_API_SERVER_KEY="test_api_server_key",
        LLM_BASE_URL="https://spark2-vllm.dalue.co/v1",
        LLM_MODEL="qwen3-next",
    )
    (tmp_path / "room_AAA").mkdir()

    _ensure_config_yaml("room_AAA", settings)

    assert not (tmp_path / "room_AAA" / "config.yaml").exists()


def test_ensure_config_yaml_writes_seeded_mcp(tmp_path: Path) -> None:
    """Every MCP seeded under the room's mcp/ dir gets an mcp_servers entry.

    Verifies that:
    - args are rewritten to the MCP's in-container seeded path
      (/opt/data/mcp/<name>/...)
    - {room_id} is substituted in the manifest's env block
    - other manifest fields (tools.exclude) pass through untouched
    - toolsets gets a mcp-<name> entry
    """
    templates_dir = tmp_path / "templates"
    _write_config_template(templates_dir)
    _write_mcp_template(
        templates_dir,
        "secretary",
        """\
command: node
args:
  - server.mjs
env:
  SECRETARY_LINE_USER_ID: "{room_id}"
tools:
  exclude:
    - line_send_message
""",
    )
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path / "data",
        HOST_DATA_DIR=tmp_path / "data",
        HERMES_TEMPLATES_DIR=templates_dir,
        HERMES_API_SERVER_KEY="test_api_server_key",
        LLM_BASE_URL="https://spark2-vllm.dalue.co/v1",
        LLM_MODEL="qwen3-next",
    )

    _ensure_mcp_seed("room_AAA", settings)
    _ensure_config_yaml("room_AAA", settings)

    written = (settings.DATA_DIR / "room_AAA" / "config.yaml").read_text(encoding="utf-8")
    assert "/opt/data/mcp/secretary/server.mjs" in written
    assert "SECRETARY_LINE_USER_ID: room_AAA" in written
    assert "line_send_message" in written
    assert "mcp-secretary" in written


def test_ensure_config_yaml_writes_default_plugins(tmp_path: Path) -> None:
    """The default plugins list is written into config.yaml under plugins.enabled."""
    templates_dir = tmp_path / "templates"
    _write_config_template(templates_dir)
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path,
        HOST_DATA_DIR=tmp_path,
        HERMES_TEMPLATES_DIR=templates_dir,
        HERMES_API_SERVER_KEY="test_api_server_key",
        LLM_BASE_URL="https://spark2-vllm.dalue.co/v1",
        LLM_MODEL="qwen3-next",
        DEFAULT_PLUGINS="local-tools",
    )
    (tmp_path / "room_AAA").mkdir()

    _ensure_config_yaml("room_AAA", settings)

    written = (tmp_path / "room_AAA" / "config.yaml").read_text(encoding="utf-8")
    assert "plugins:" in written
    assert "- local-tools" in written


def test_ensure_config_yaml_does_not_overwrite_existing(tmp_path: Path) -> None:
    """An existing config.yaml is left untouched so hand-edits survive recreation."""
    room_dir = tmp_path / "room_AAA"
    room_dir.mkdir(parents=True)
    config_path = room_dir / "config.yaml"
    config_path.write_text("# hand-edited\n", encoding="utf-8")
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path,
        HOST_DATA_DIR=tmp_path,
        HERMES_API_SERVER_KEY="test_api_server_key",
        LLM_BASE_URL="https://spark2-vllm.dalue.co/v1",
        LLM_MODEL="qwen3-next",
    )

    _ensure_config_yaml("room_AAA", settings)

    assert config_path.read_text(encoding="utf-8") == "# hand-edited\n"


def test_wait_until_ready_returns_once_health_check_succeeds() -> None:
    """Polling stops as soon as /health returns 200, without hitting the timeout."""
    mock_response = MagicMock(status_code=200)
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with patch("alice_office_router.container_manager.httpx.Client", return_value=mock_client):
        _wait_until_ready("http://hermes_room_AAA:8642")

    mock_client.get.assert_called_once_with("http://hermes_room_AAA:8642/health")


def test_wait_until_ready_raises_on_timeout() -> None:
    """If the health check never succeeds, raise RuntimeError instead of hanging forever."""
    mock_client = MagicMock()
    mock_client.get.side_effect = httpx.ConnectError("refused")
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False

    with (
        patch("alice_office_router.container_manager.httpx.Client", return_value=mock_client),
        patch("alice_office_router.container_manager.time.sleep"),
        pytest.raises(RuntimeError, match="did not become ready"),
    ):
        _wait_until_ready("http://hermes_room_AAA:8642", timeout=0.01)


def test_docker_api_error_is_raised() -> None:
    """When Docker API raises APIError, it should propagate after logging."""
    mock_client = MagicMock()
    mock_client.containers.get.side_effect = docker.errors.APIError("boom")

    with (
        patch("alice_office_router.container_manager.docker.from_env", return_value=mock_client),
        pytest.raises(docker.errors.APIError),
    ):
        get_or_create_container("room_AAA", SETTINGS_IN_DOCKER)


# ---------------------------------------------------------------------------
# Google OAuth integration — {account_key} substitution, gated seeding,
# conditional volume mount, requires_google_oauth key stripping.
# ---------------------------------------------------------------------------


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


def test_account_key_substitution_lowercases_room_id(tmp_path: Path) -> None:
    """{account_key} in a manifest is substituted with the lowercased room id."""
    templates_dir = tmp_path / "templates"
    _write_mcp_template(
        templates_dir,
        "google-calendar",
        'command: google-calendar-mcp\nargs: []\nenv:\n  GOOGLE_ACCOUNT_MODE: "{account_key}"\n',
    )
    settings = _settings_with_google(tmp_path, enabled=True)
    _write_config_template(settings.HERMES_TEMPLATES_DIR)

    _ensure_mcp_seed("U_ROOM_ABC", settings)
    _ensure_config_yaml("U_ROOM_ABC", settings)

    written = (settings.DATA_DIR / "U_ROOM_ABC" / "config.yaml").read_text(encoding="utf-8")
    assert "GOOGLE_ACCOUNT_MODE: u_room_abc" in written


def test_requires_google_oauth_key_not_in_rendered_mcp_section(tmp_path: Path) -> None:
    """The manifest-only `requires_google_oauth` key must not leak into config.yaml."""
    templates_dir = tmp_path / "templates"
    _write_mcp_template(
        templates_dir,
        "gmail",
        "command: /opt/tools/.venv/bin/python3\n"
        "args: [server.py]\n"
        'env:\n  GOOGLE_ACCOUNT_MODE: "{account_key}"\n'
        "requires_google_oauth: true\n",
    )
    settings = _settings_with_google(tmp_path, enabled=True)
    _write_config_template(settings.HERMES_TEMPLATES_DIR)

    _ensure_mcp_seed("room_AAA", settings)
    _ensure_config_yaml("room_AAA", settings)

    written = (settings.DATA_DIR / "room_AAA" / "config.yaml").read_text(encoding="utf-8")
    assert "requires_google_oauth" not in written


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

    _ensure_mcp_seed("room_AAA", settings)

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

    _ensure_mcp_seed("room_AAA", settings)

    assert (settings.DATA_DIR / "room_AAA" / "mcp" / "gmail").exists()


def test_volume_config_adds_google_mount_only_when_enabled(tmp_path: Path) -> None:
    """The shared Google credentials dir is mounted only when google_oauth_enabled is True."""
    enabled_settings = _settings_with_google(tmp_path, enabled=True)
    disabled_settings = _settings_with_google(tmp_path, enabled=False)

    enabled_volumes = _build_volume_config("room_AAA", enabled_settings)
    disabled_volumes = _build_volume_config("room_AAA", disabled_settings)

    assert str(enabled_settings.google_host_dir) in enabled_volumes
    assert enabled_volumes[str(enabled_settings.google_host_dir)] == {
        "bind": CONTAINER_GOOGLE_DIR,
        "mode": "rw",
    }
    assert str(disabled_settings.google_host_dir) not in disabled_volumes
