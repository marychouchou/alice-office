from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import docker.errors
import httpx
import pytest

from alice_office_router.config import Settings
from alice_office_router.container_manager import (
    _ensure_config_yaml,
    _wait_until_ready,
    get_or_create_container,
)

SETTINGS_IN_DOCKER = Settings(
    LINE_CHANNEL_SECRET="test_secret",
    LINE_CHANNEL_ACCESS_TOKEN="test_token",
    DATA_DIR=Path("/tmp/test_data"),
    HOST_DATA_DIR=Path("/tmp/test_data"),
    HOST_PLUGINS_DIR=Path("/tmp/test_plugins"),
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
    HOST_PLUGINS_DIR=Path("/tmp/test_plugins"),
    HERMES_INTERNAL_PORT=8642,
    HERMES_API_SERVER_KEY="test_api_server_key",
    ROUTER_IN_DOCKER=False,
)

CONTAINER_NAME = "hermes_room_AAA"
EXPECTED_URL_DOCKER = f"http://{CONTAINER_NAME}:8642"
EXPECTED_URL_HOST = "http://localhost:54321"


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
        "/tmp/test_plugins": {"bind": "/opt/data/plugins", "mode": "ro"},
    }
    env = call_kwargs["environment"]
    assert env["API_SERVER_KEY"] == "test_api_server_key"
    assert env["API_SERVER_HOST"] == "0.0.0.0"
    assert env["LLM_API_KEY"] == "sk-test"
    assert "LINE_CHANNEL_ACCESS_TOKEN" not in env
    assert "LINE_CHANNEL_SECRET" not in env


def test_missing_container_mounts_secretary_mcp_dev_override_when_set() -> None:
    """When HOST_SECRETARY_MCP_DIR is set, server.mjs + tools/ are bind-mounted over the image."""
    mock_container = _make_running_container()
    mock_client = MagicMock()
    mock_client.containers.get.side_effect = docker.errors.NotFound("not found")
    mock_client.containers.run.return_value = mock_container
    settings = SETTINGS_IN_DOCKER.model_copy(
        update={"HOST_SECRETARY_MCP_DIR": "/tmp/test_secretary_mcp"}
    )

    with (
        patch("alice_office_router.container_manager.docker.from_env", return_value=mock_client),
        patch("alice_office_router.container_manager._wait_until_ready"),
        patch("alice_office_router.container_manager._ensure_data_dir"),
        patch("alice_office_router.container_manager._ensure_config_yaml"),
    ):
        get_or_create_container("room_AAA", settings)

    volumes = mock_client.containers.run.call_args.kwargs["volumes"]
    assert volumes["/tmp/test_secretary_mcp/server.mjs"] == {
        "bind": "/opt/secretary-mcp/server.mjs",
        "mode": "ro",
    }
    assert volumes["/tmp/test_secretary_mcp/tools"] == {
        "bind": "/opt/secretary-mcp/tools",
        "mode": "ro",
    }


def test_missing_container_skips_secretary_mcp_mount_by_default() -> None:
    """When HOST_SECRETARY_MCP_DIR is unset (production default), no extra mount is added."""
    mock_container = _make_running_container()
    mock_client = MagicMock()
    mock_client.containers.get.side_effect = docker.errors.NotFound("not found")
    mock_client.containers.run.return_value = mock_container

    with (
        patch("alice_office_router.container_manager.docker.from_env", return_value=mock_client),
        patch("alice_office_router.container_manager._wait_until_ready"),
        patch("alice_office_router.container_manager._ensure_data_dir"),
        patch("alice_office_router.container_manager._ensure_config_yaml"),
    ):
        get_or_create_container("room_AAA", SETTINGS_IN_DOCKER)

    volumes = mock_client.containers.run.call_args.kwargs["volumes"]
    assert not any("secretary-mcp" in bind["bind"] for bind in volumes.values())


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
        patch("alice_office_router.container_manager._ensure_config_yaml"),
        patch("alice_office_router.container_manager._find_free_port", return_value=54321),
    ):
        url = get_or_create_container("room_AAA", SETTINGS_ON_HOST)

    assert url == EXPECTED_URL_HOST
    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["ports"] == {"8642/tcp": 54321}


def test_ensure_config_yaml_writes_provider_block(tmp_path: Path) -> None:
    """When no config.yaml exists yet, one is written with the shared LLM provider."""
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path,
        HOST_DATA_DIR=tmp_path,
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


def test_ensure_config_yaml_writes_secretary_mcp(tmp_path: Path) -> None:
    """The secretary MCP server block is written into config.yaml.

    Verifies that:
    - mcp_servers.secretary points at /opt/secretary-mcp/server.mjs
    - room_id is templated as SECRETARY_LINE_USER_ID (per-room state key)
    - The ${GOOGLE_MAPS_API_KEY} placeholder is preserved literally for
      Hermes to expand at MCP spawn time (not consumed by str.format)
    - LINE and reminder tools are excluded (router owns LINE communication)
    """
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path,
        HOST_DATA_DIR=tmp_path,
        HERMES_API_SERVER_KEY="test_api_server_key",
        LLM_BASE_URL="https://spark2-vllm.dalue.co/v1",
        LLM_MODEL="qwen3-next",
    )
    (tmp_path / "room_AAA").mkdir()

    _ensure_config_yaml("room_AAA", settings)

    written = (tmp_path / "room_AAA" / "config.yaml").read_text(encoding="utf-8")
    assert "/opt/secretary-mcp/server.mjs" in written
    assert "SECRETARY_LINE_USER_ID: room_AAA" in written
    # Single-brace placeholder for Hermes' own ${VAR} expansion (not pre-resolved).
    assert "${GOOGLE_MAPS_API_KEY}" in written
    assert "line_send_message" in written
    assert "line_send_media" in written
    assert "line_send_file" in written
    assert "reminder_set" in written
    assert "mcp-secretary" in written


def test_ensure_config_yaml_writes_default_plugins(tmp_path: Path) -> None:
    """The default plugins list is written into config.yaml under plugins.enabled."""
    settings = Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        DATA_DIR=tmp_path,
        HOST_DATA_DIR=tmp_path,
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
