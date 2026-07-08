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
    # Optional: forwarded into each Hermes container so secretary-mcp's
    # maps_search / maps_details tools can call Google Places API (New).
    # Leave blank to disable maps tools (they return a config error when called).
    GOOGLE_MAPS_API_KEY: str = ""
    # Host-side path to the plugins directory. Used for Docker volume
    # mounting into each Hermes container (mirrors HOST_DATA_DIR).
    # In Docker mode, set this to the absolute host path (compose uses ${PWD}/plugins).
    HOST_PLUGINS_DIR: Path = Path("plugins")
    # Dev-only: host-side path to the secretary-mcp/ source directory. When
    # set, server.mjs + tools/ are bind-mounted (read-only) over the copy
    # baked into the Hermes image at /opt/secretary-mcp/, so source edits
    # take effect on container restart without a rebuild. node_modules stays
    # the image's baked-in copy (not mounted). Leave blank in production —
    # customer hosts won't have this repo's source tree available.
    HOST_SECRETARY_MCP_DIR: str = ""
    # Comma-separated plugin names written into every new room's config.yaml
    # under plugins.enabled. These become default tools for all containers.
    DEFAULT_PLUGINS: str = "local-tools"


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Returns:
        Settings: Application settings loaded from environment.
    """
    return Settings()  # type: ignore[call-arg]
