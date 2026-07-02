from __future__ import annotations

import logging
import socket
import threading
import time

import docker
import docker.errors
import docker.models.containers
import httpx

from alice_office_router.config import Settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# The real Hermes Agent image boots through s6 supervision, skill sync, and
# gateway startup — much slower than the previous mock's instant FastAPI app.
_READY_TIMEOUT_SECONDS = 60.0
_READY_POLL_INTERVAL_SECONDS = 1.0


def _find_free_port() -> int:
    """Bind to port 0 to let the OS pick a free port, then return it.

    Returns:
        An available TCP port number on the host.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return int(sock.getsockname()[1])


def _ensure_data_dir(room_id: str, config: Settings) -> None:
    """Create the local data directory for a room if it does not exist.

    Args:
        room_id: Unique identifier for the chatroom.
        config: Application settings containing the data directory path.
    """
    data_path = config.DATA_DIR / room_id
    data_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Ensured data directory exists: {data_path}")


def _build_container_env(config: Settings) -> dict[str, str]:
    """Build environment variable dict for a new Hermes agent container.

    The container never receives LINE credentials — the router owns all LINE
    communication and only talks to the agent through its api_server platform.

    Args:
        config: Application settings with the api_server secret and LLM credentials.

    Returns:
        Dictionary of environment variable names to values.
    """
    env: dict[str, str] = {
        "API_SERVER_KEY": config.HERMES_API_SERVER_KEY,
        # api_server binds 127.0.0.1 by default; the router reaches it from
        # another container on the same Docker network, so it must bind all interfaces.
        "API_SERVER_HOST": "0.0.0.0",
    }
    if config.LLM_API_KEY:
        env["LLM_API_KEY"] = config.LLM_API_KEY
    return env


def _build_volume_config(room_id: str, config: Settings) -> dict[str, dict[str, str]]:
    """Build Docker volume configuration for a room's container.

    Args:
        room_id: Unique identifier for the chatroom.
        config: Application settings containing host data directory path.

    Returns:
        Docker volumes mapping dict for use with containers.run().
    """
    host_path = str(config.HOST_DATA_DIR / room_id)
    return {host_path: {"bind": "/opt/data", "mode": "rw"}}


_CONFIG_YAML_TEMPLATE = """\
model:
  default: {model}
  provider: custom
providers:
  custom:
    base_url: {base_url}
    key_env: LLM_API_KEY
    default_model: {model}
    models:
      - {model}
"""


def _ensure_config_yaml(room_id: str, config: Settings) -> None:
    """Write a default Hermes config.yaml for a room if one doesn't already exist.

    Configures the shared LLM provider so the agent can answer without any
    manual per-room setup. Left untouched on subsequent calls so operators can
    hand-edit a room's config without it being overwritten on container recreation.

    Args:
        room_id: Unique identifier for the chatroom.
        config: Application settings containing the shared LLM provider details.
    """
    config_path = config.DATA_DIR / room_id / "config.yaml"
    if config_path.exists() or not config.LLM_BASE_URL or not config.LLM_MODEL:
        return
    config_path.write_text(
        _CONFIG_YAML_TEMPLATE.format(model=config.LLM_MODEL, base_url=config.LLM_BASE_URL),
        encoding="utf-8",
    )
    logger.info(f"Wrote default config.yaml for room [{room_id}]")


def _wait_until_ready(url: str, timeout: float = _READY_TIMEOUT_SECONDS) -> None:
    """Poll a Hermes agent's health endpoint until it responds or times out.

    Args:
        url: Base URL of the Hermes agent container.
        timeout: Maximum seconds to wait before giving up.

    Raises:
        RuntimeError: If the agent does not become healthy within the timeout.
    """
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(f"{url}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(_READY_POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"Hermes agent at {url} did not become ready within {timeout}s")


def _get_container_url(
    container: docker.models.containers.Container,
    config: Settings,
) -> str:
    """Resolve the URL the router should use to reach a container.

    When the router is inside Docker it uses the container's hostname on the
    shared network.  When the router runs on the host (macOS dev mode) it reads
    the dynamically published host port instead.

    Args:
        container: Running Docker container object.
        config: Application settings.

    Returns:
        HTTP URL string for the container's webhook endpoint.

    Raises:
        RuntimeError: If host-port mode is active but no port mapping is found.
    """
    if config.ROUTER_IN_DOCKER:
        return f"http://{container.name}:{config.HERMES_INTERNAL_PORT}"

    container.reload()
    mappings = container.ports.get(f"{config.HERMES_INTERNAL_PORT}/tcp") or []
    if not mappings:
        raise RuntimeError(
            f"Container {container.name} has no published port {config.HERMES_INTERNAL_PORT}"
        )
    host_port = mappings[0]["HostPort"]
    return f"http://localhost:{host_port}"


def _create_container(
    client: docker.DockerClient,
    container_name: str,
    room_id: str,
    config: Settings,
) -> docker.models.containers.Container:
    """Create and start a new Hermes agent Docker container.

    Publishes HERMES_INTERNAL_PORT to a random host port when ROUTER_IN_DOCKER
    is False, so the host-side router can reach it via localhost.

    Args:
        client: Docker client instance.
        container_name: Name to assign to the new container.
        room_id: Unique identifier for the chatroom.
        config: Application settings.

    Returns:
        The newly created (and running) Container object.
    """
    logger.info(f"Creating new container for room [{room_id}]: {container_name}")
    _ensure_data_dir(room_id, config)
    _ensure_config_yaml(room_id, config)

    ports: dict[str, int] | None = None
    if not config.ROUTER_IN_DOCKER:
        host_port = _find_free_port()
        ports = {f"{config.HERMES_INTERNAL_PORT}/tcp": host_port}
        logger.info(f"Publishing container port {config.HERMES_INTERNAL_PORT} → host:{host_port}")

    container: docker.models.containers.Container = client.containers.run(
        image=config.HERMES_IMAGE,
        name=container_name,
        command=["gateway", "run"],
        detach=True,
        restart_policy={"Name": "always"},
        environment=_build_container_env(config),
        volumes=_build_volume_config(room_id, config),
        network=config.HERMES_NETWORK,
        ports=ports,
    )
    logger.info(f"Container {container_name} created.")
    return container


def get_or_create_container(room_id: str, config: Settings) -> str:
    """Return the URL for the Hermes agent container for a given room.

    If the container does not exist, it is created and started automatically.
    If the container exists but is stopped, it is restarted. Uses a module-level
    lock to prevent race conditions during concurrent requests for the same room.
    When a container was just created or restarted, blocks until its api_server
    health check responds before returning, since the Hermes gateway takes
    longer to boot than a simple process start.

    Args:
        room_id: Unique identifier for the chatroom (used for container name and data dir).
        config: Application settings.

    Returns:
        Internal or host-facing HTTP URL of the Hermes agent container.

    Raises:
        docker.errors.APIError: If a Docker API call fails.
        RuntimeError: If the container URL cannot be resolved, or the agent
            does not become ready within the startup timeout.
    """
    container_name = f"hermes_{room_id}"
    needs_wait = False

    with _lock:
        client: docker.DockerClient = docker.from_env()
        try:
            container = client.containers.get(container_name)
            if container.status != "running":
                logger.info(f"Container {container_name} is stopped; restarting.")
                container.start()
                container.reload()
                needs_wait = True
            else:
                logger.debug(f"Container {container_name} already running.")
        except docker.errors.NotFound:
            container = _create_container(client, container_name, room_id, config)
            needs_wait = True
        except docker.errors.APIError as exc:
            logger.error(f"Docker API error for container {container_name}: {exc}")
            raise

    url = _get_container_url(container, config)
    if needs_wait:
        logger.info(f"Waiting for {container_name} to become ready...")
        _wait_until_ready(url)
    return url
