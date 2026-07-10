from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    LINE_CHANNEL_SECRET: str
    LINE_CHANNEL_ACCESS_TOKEN: str
    DATA_DIR: Path = Path("/app/data")
    HOST_DATA_DIR: Path = Path("/app/data")
    HERMES_IMAGE: str = "nousresearch/hermes-agent"
    HERMES_NETWORK: str = "hermes_global_net"
    # Hermes Agent's built-in OpenAI-compatible api_server platform port.
    HERMES_INTERNAL_PORT: int = 8642
    # Shared bearer secret between the router and every Hermes agent
    # container's api_server platform (sets API_SERVER_KEY in the container).
    HERMES_API_SERVER_KEY: str
    # Set False when router runs on the host (not inside Docker).
    # Containers will publish port 8642 to a random host port so the host
    # can reach them via localhost instead of Docker-internal DNS.
    ROUTER_IN_DOCKER: bool = True
    # LLM endpoint forwarded into every Hermes agent container.
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = ""
    # Router-local path (like DATA_DIR — the router's own filesystem view,
    # NOT a host path for Docker volume mounting) to this repo's src/hermes/
    # directory. Holds mcp/<name>/ and plugin/<name>/ source templates —
    # _ensure_mcp_seed / _ensure_plugin_seed copy each one into a room's
    # data dir (data/<room_id>/mcp/<name>/, data/<room_id>/plugins/<name>/)
    # the first time that room's container is created, then never touch it
    # again — the room's copy is the room's own to edit from then on. Also
    # holds config.yaml.template (see _ensure_config_yaml), which unlike
    # those is a str.format() template rather than something copied verbatim.
    # In Docker mode, docker-compose.yml mounts ./src/hermes here read-only.
    # Host-dev mode must point this at the repo's actual src/hermes path.
    HERMES_TEMPLATES_DIR: Path = Path("/app/hermes-templates")
    # Comma-separated plugin names written into every new room's config.yaml
    # under plugins.enabled. These become default tools for all containers.
    # Names must match seeded plugin directory names under HERMES_TEMPLATES_DIR/plugin/.
    DEFAULT_PLUGINS: str = "local-tools"


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Returns:
        Settings: Application settings loaded from environment.
    """
    return Settings()  # type: ignore[call-arg]
