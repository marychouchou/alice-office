from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any

import docker
import docker.errors
import docker.models.containers
import docker.types
import httpx
import yaml
from structlog.contextvars import bound_contextvars

from alice_office_router.config import Settings
from alice_office_router.room_seed import (
    ensure_google_seed,
    ensure_mcp_seed,
    ensure_plugin_seed,
    ensure_soul_seed,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# The real Hermes Agent image boots through s6 supervision, skill sync, and
# gateway startup — much slower than the previous mock's instant FastAPI app.
_READY_TIMEOUT_SECONDS = 60.0
_READY_POLL_INTERVAL_SECONDS = 1.0

# Bind path for each room's data volume inside its Hermes container. Exported
# so callers outside this module (e.g. channels/line/events.py, when telling
# the agent where an inbound media file landed) can reference the same path
# without duplicating the string.
CONTAINER_DATA_DIR = "/opt/data"

# Path (inside CONTAINER_DATA_DIR, so already covered by its rw bind mount —
# no separate mount needed) where each room's own seeded copy of every
# enabled plugin's source lives. Room-specific plugin runtime data (SQLite
# DBs, caches, etc.) lives alongside it, e.g. /opt/data/local-tools-data/.
CONTAINER_PLUGINS_DIR = f"{CONTAINER_DATA_DIR}/plugins"

# Path (also inside CONTAINER_DATA_DIR) where each room's own seeded copy of
# every configured MCP server's source lives — see room_seed.ensure_mcp_seed.
# Node ESM dependency resolution for these walks up from here to
# /opt/node_modules (baked into the image by Dockerfile.hermes; NODE_PATH is
# ignored by ESM).
CONTAINER_MCP_DIR = f"{CONTAINER_DATA_DIR}/mcp"

# In-container mount path for a room's own Google OAuth tokens + credentials
# directory (config.room_google_host_dir(room_id) on the host — each room
# gets its own host directory, but every room's container mounts it at this
# same in-container path). A neutral, world-readable mount point deliberately
# OUTSIDE both /root and /opt/data, because (verified in a live room container):
#   (a) the Hermes gateway and every MCP subprocess run as user `hermes`
#       (uid 10000), NOT root — /root is mode 700, so anything under
#       /root/... is EACCES-unreachable to them;
#   (b) the gateway sets XDG_CONFIG_HOME=/opt/data/.config, so a tool's
#       "default" config path would silently resolve per-room instead of
#       to this directory.
# Consequently NOTHING relies on default paths: every consumer (the @cocal
# calendar MCP, gmail/drive token_manager.py) receives explicit
# env-provided paths under this directory via its mcp.manifest.yaml.
CONTAINER_GOOGLE_DIR = "/opt/google-workspace"

# Docker labels every room container carries, so a log collector can tell which
# room a stdout line belongs to without parsing it — Alloy's discovery.docker
# filters and relabels on exactly these (docs/logging-design.md §5.2). The
# channel is not a separate label: a room key is `<channel>_<native id>`, so
# alice.room_id already carries it. Labels are set at creation time only: a
# container created before this existed must be `docker rm -f`'d for the router
# to recreate it with them.
_LABEL_ROLE = "alice.role"
_LABEL_ROOM_ID = "alice.room_id"

# Per-container stdout cap. Docker's json-file driver does NOT rotate by
# default, so an idle-but-long-lived room would grow without bound; 10m x 3
# keeps each room under 30 MB and leaves `docker logs` working exactly as
# before (the troubleshooting flows all read through it).
_LOG_MAX_SIZE = "10m"
_LOG_MAX_FILE = "3"


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

    Always mounts the room's data directory (read-write, room-isolated) at
    CONTAINER_DATA_DIR. Plugin and MCP server source used to be separate
    shared, read-only mounts; both are now seeded once into this same data
    directory instead (see room_seed.ensure_plugin_seed,
    room_seed.ensure_mcp_seed), so each room can edit its own copy
    independently — no extra mount is needed since they already live under
    the rw mount.

    When Google OAuth is configured for this deployment, also mounts this
    room's own tokens/credentials directory (config.room_google_host_dir,
    seeded once by room_seed.ensure_google_seed) at CONTAINER_GOOGLE_DIR — fully
    isolated per room, unlike an earlier design that shared one directory
    across every room's container. CONTAINER_GOOGLE_DIR is a neutral path
    outside /root and /opt/data: MCP subprocesses run as uid 10000 `hermes`
    (can't traverse /root, mode 700), and the gateway's
    XDG_CONFIG_HOME=/opt/data/.config would redirect "default" config paths
    per-room — so all consumers are given explicit env paths under this
    mount instead (see each Google MCP's mcp.manifest.yaml). This mount is
    only added at container *creation* time — toggling Google OAuth on/off
    requires recreating already-existing room containers to pick up the change.

    Args:
        room_id: Unique identifier for the chatroom.
        config: Application settings containing the host data path.

    Returns:
        Docker volumes mapping dict for use with containers.run().
    """
    host_data_path = str(config.HOST_DATA_DIR / room_id)
    volumes = {
        host_data_path: {"bind": CONTAINER_DATA_DIR, "mode": "rw"},
    }
    if config.google_oauth_enabled:
        volumes[str(config.room_google_host_dir(room_id))] = {
            "bind": CONTAINER_GOOGLE_DIR,
            "mode": "rw",
        }
    return volumes


def _format_plugins_yaml(plugins: list[str]) -> str:
    """Format a list of plugin names as a YAML plugins section.

    Args:
        plugins: List of plugin names to enable.

    Returns:
        YAML string for the ``plugins:`` config block.
    """
    if not plugins:
        return "plugins:\n  enabled: []"
    items = "\n".join(f"    - {p}" for p in plugins)
    return f"plugins:\n  enabled:\n{items}"


def _load_mcp_manifest(mcp_dir: Path, room_id: str) -> dict[str, Any] | None:
    """Load one MCP's seeded mcp.manifest.yaml, substituting {room_id}/{account_key}.

    A missing or malformed manifest is logged and skipped rather than
    raised — one broken MCP must not stop config.yaml from being written
    for the room's other MCPs (or with no MCPs at all).

    Args:
        mcp_dir: The room's seeded copy of one MCP, e.g. data/<room_id>/mcp/secretary/.
        room_id: Unique chatroom identifier substituted for ``{room_id}`` in
            the manifest (e.g. secretary-mcp's per-user state key).
            ``{account_key}`` is also substituted, with the room id
            lowercased — required by @cocal/google-calendar-mcp's
            lowercase-only GOOGLE_ACCOUNT_MODE validation (and used
            consistently by the router's Google OAuth token store).

    Returns:
        The parsed manifest mapping, or None if it could not be loaded.
    """
    manifest_path = mcp_dir / "mcp.manifest.yaml"
    if not manifest_path.exists():
        logger.error(f"MCP [{mcp_dir.name}] has no mcp.manifest.yaml; skipping")
        return None
    try:
        raw = (
            manifest_path.read_text(encoding="utf-8")
            .replace("{room_id}", room_id)
            .replace("{account_key}", room_id.lower())
        )
        manifest = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        logger.error(f"Failed to load MCP manifest {manifest_path}: {exc}")
        return None
    if not isinstance(manifest, dict):
        logger.error(f"MCP manifest {manifest_path} is not a mapping; skipping")
        return None
    return manifest


def _format_mcp_section(room_id: str, config: Settings) -> str:
    """Render the mcp_servers / toolsets block for a room's config.yaml.

    Reads every MCP already seeded under data/<room_id>/mcp/
    (room_seed.ensure_mcp_seed must run first) and emits one
    mcp_servers.<name> entry per MCP, with args
    rewritten to that MCP's in-container seeded path (CONTAINER_MCP_DIR/<name>/...).

    Args:
        room_id: Unique chatroom identifier, substituted into each MCP's
            manifest (e.g. secretary-mcp's per-user state key).
        config: Application settings containing the room's data directory.

    Returns:
        YAML string for the ``mcp_servers`` / ``toolsets`` blocks, or an
        empty string if the room has no seeded MCPs.
    """
    mcp_root = config.DATA_DIR / room_id / "mcp"
    if not mcp_root.is_dir():
        return ""

    servers: dict[str, Any] = {}
    toolsets: list[str] = []
    for mcp_dir in sorted(mcp_root.iterdir()):
        if not mcp_dir.is_dir():
            continue
        manifest = _load_mcp_manifest(mcp_dir, room_id)
        if manifest is None:
            continue
        name = mcp_dir.name
        args = [f"{CONTAINER_MCP_DIR}/{name}/{arg}" for arg in manifest.get("args", [])]
        servers[name] = {
            "command": manifest.get("command", "node"),
            "args": args,
            **{
                k: v
                for k, v in manifest.items()
                if k not in ("command", "args", "requires_google_oauth")
            },
        }
        toolsets.append(f"mcp-{name}")

    if not servers:
        return ""
    section = {"mcp_servers": servers, "toolsets": toolsets}
    return str(yaml.safe_dump(section, sort_keys=False, default_flow_style=False))


# Filename of the config.yaml template under HERMES_TEMPLATES_DIR (repo path:
# src/hermes/config.template.yml), alongside the mcp/ and plugin/ template
# dirs. Unlike those, it isn't seeded verbatim — it's a str.format() template
# with {model}/{base_url}/{plugins_section}/{mcp_section} placeholders filled
# in at write time, since a room's actual values (LLM settings, which MCPs
# got seeded) aren't known until then.
_CONFIG_YAML_TEMPLATE_FILENAME = "config.template.yml"


def _ensure_config_yaml(room_id: str, config: Settings) -> None:
    """Write a default Hermes config.yaml for a room if one doesn't already exist.

    Configures the shared LLM provider, default plugins, and every MCP
    server already seeded for this room (see room_seed.ensure_mcp_seed,
    which must run first) so the agent can answer without any manual
    per-room setup.
    Left untouched on subsequent calls so operators can hand-edit a room's
    config without it being overwritten on container recreation.

    Args:
        room_id: Unique identifier for the chatroom (also each MCP's
            per-user state key, via {room_id} substitution in its manifest).
        config: Application settings containing the shared LLM provider details
            and default plugin list.
    """
    config_path = config.DATA_DIR / room_id / "config.yaml"
    if config_path.exists() or not config.LLM_BASE_URL or not config.LLM_MODEL:
        return
    template_path = config.HERMES_TEMPLATES_DIR / _CONFIG_YAML_TEMPLATE_FILENAME
    if not template_path.exists():
        logger.error(f"Missing config.yaml template at {template_path}; skipping room [{room_id}]")
        return
    plugins = [p.strip() for p in config.DEFAULT_PLUGINS.split(",") if p.strip()]
    config_path.write_text(
        template_path.read_text(encoding="utf-8").format(
            model=config.LLM_MODEL,
            base_url=config.LLM_BASE_URL,
            plugins_section=_format_plugins_yaml(plugins),
            mcp_section=_format_mcp_section(room_id, config),
        ),
        encoding="utf-8",
    )
    logger.info(f"Wrote default config.yaml for room [{room_id}]")


def _probe_health(client: httpx.Client, url: str) -> str | None:
    """Probe a Hermes agent's health endpoint once.

    Args:
        client: The HTTP client to poll with.
        url: Base URL of the Hermes agent container.

    Returns:
        None when the endpoint answered 200; otherwise a short, content-free
        reason ("ConnectError", "status 503") the caller logs and folds into
        its timeout error, so an operator can tell "nothing listening yet"
        from "listening but unhealthy".
    """
    try:
        response = client.get(f"{url}/health")
    except httpx.HTTPError as exc:
        return type(exc).__name__
    return None if response.status_code == 200 else f"status {response.status_code}"


def _wait_until_ready(url: str, timeout: float = _READY_TIMEOUT_SECONDS) -> None:
    """Poll a Hermes agent's health endpoint until it responds or times out.

    A container that is already healthy answers on the first poll, so calling
    this on every resolution costs one HTTP GET, not a wait.

    Args:
        url: Base URL of the Hermes agent container.
        timeout: Maximum seconds to wait before giving up.

    Raises:
        RuntimeError: If the agent does not become healthy within the timeout.
    """
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5.0) as client:
        reason = _probe_health(client, url)
        if reason is None:
            return
        # Only a container that is actually booting (or broken) gets past the
        # first probe, so this line stays per-start, not per-turn.
        logger.info(f"Waiting for Hermes agent at {url} to become ready ({reason})...")
        while time.monotonic() < deadline:
            time.sleep(_READY_POLL_INTERVAL_SECONDS)
            reason = _probe_health(client, url)
            if reason is None:
                return
    raise RuntimeError(
        f"Hermes agent at {url} did not become ready within {timeout}s (last: {reason})"
    )


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
    # Independent of the seed steps below, but must land before
    # containers.run: Hermes writes its own generic default SOUL.md on first
    # boot if the file is absent, and (like every write-once seed) never
    # overwrites it afterwards — a late seed would never take effect.
    ensure_soul_seed(room_id, config)
    # MCP/plugin seeding must happen before config.yaml is written:
    # _format_mcp_section reads each MCP's manifest from its just-seeded
    # directory to know what to register.
    ensure_mcp_seed(room_id, config)
    ensure_plugin_seed(room_id, config)
    _ensure_config_yaml(room_id, config)
    # No-ops (already seeded) if this room reached ensure_google_seed earlier
    # via /oauth/start — a gate-blocked turn only *starts* this container in
    # the background, so the user may click the auth link before it exists.
    ensure_google_seed(room_id, config)

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
        labels={
            _LABEL_ROLE: "agent",
            _LABEL_ROOM_ID: room_id,
        },
        log_config=docker.types.LogConfig(
            type=docker.types.LogConfig.types.JSON,
            config={"max-size": _LOG_MAX_SIZE, "max-file": _LOG_MAX_FILE},
        ),
    )
    logger.info(f"Container {container_name} created.")
    return container


def _resolve_container(
    client: docker.DockerClient,
    container_name: str,
    room_id: str,
    config: Settings,
) -> docker.models.containers.Container:
    """Get the room's container, starting or creating it when it isn't running.

    Split out of get_or_create_container so that function stays within the
    nesting budget once it binds the room's log context; the caller holds the
    module lock around this call.

    Args:
        client: Docker client instance.
        container_name: Name of this room's container (hermes_<room_id>).
        room_id: Unique identifier for the chatroom.
        config: Application settings.

    Returns:
        The running Container. The caller always waits for its api_server
        health check afterwards, whichever path produced it.

    Raises:
        docker.errors.APIError: If a Docker API call fails.
    """
    # start()/reload() belong INSIDE this try, not after it: a container that
    # was removed between the get and the start (an operator running
    # `docker rm`, exactly what the Phase 2 relabelling note tells them to do)
    # makes start() raise NotFound, which must fall through to the create path
    # instead of escaping as an unhandled error. The same goes for any other
    # APIError from those two calls — it gets logged with the container name
    # before it propagates, like every other Docker failure here.
    try:
        container = client.containers.get(container_name)
        if container.status != "running":
            logger.info(f"Container {container_name} is stopped; restarting.")
            container.start()
            container.reload()
            return container
        logger.debug(f"Container {container_name} already running.")
        return container
    except docker.errors.NotFound:
        return _create_container(client, container_name, room_id, config)
    except docker.errors.APIError as exc:
        logger.error(f"Docker API error for container {container_name}: {exc}")
        raise


def get_or_create_container(room_id: str, config: Settings) -> str:
    """Return the URL for the Hermes agent container for a given room.

    If the container does not exist, it is created and started automatically.
    If the container exists but is stopped, it is restarted. Uses a module-level
    lock to prevent race conditions during concurrent requests for the same room.
    Always blocks until the api_server health check responds before returning:
    the Hermes gateway takes longer to boot than a simple process start, and a
    container that is "running" is not proof of readiness — one another caller
    just created (core's gate-blocked warm-up runs outside the room lock), or
    one an operator just `docker restart`ed to apply a config change, shows as
    running long before it answers. A healthy container answers the first
    poll, so the steady-state cost is one HTTP GET per turn. The accepted
    trade-off: a running container whose api_server is permanently down now
    costs the full timeout before the turn fails, where it used to fail on the
    first connection — the room hears the same failure notice either way.

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

    with bound_contextvars(container=container_name):
        with _lock:
            client: docker.DockerClient = docker.from_env()
            container = _resolve_container(client, container_name, room_id, config)

        url = _get_container_url(container, config)
        _wait_until_ready(url)
        return url
