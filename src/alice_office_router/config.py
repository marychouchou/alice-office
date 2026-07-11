from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Container-only defaults for DATA_DIR/HERMES_TEMPLATES_DIR (see their field
# docs below). Host-mode dev must override both — see _validate_host_mode_paths.
_DOCKER_DEFAULT_DATA_DIR = Path("/app/data")
_DOCKER_DEFAULT_HERMES_TEMPLATES_DIR = Path("/app/hermes-templates")


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    LINE_CHANNEL_SECRET: str
    LINE_CHANNEL_ACCESS_TOKEN: str
    DATA_DIR: Path = _DOCKER_DEFAULT_DATA_DIR
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
    # holds config.template.yml (see _ensure_config_yaml), which unlike
    # those is a str.format() template rather than something copied verbatim.
    # In Docker mode, docker-compose.yml mounts ./src/hermes here read-only.
    # Host-dev mode must point this at the repo's actual src/hermes path.
    HERMES_TEMPLATES_DIR: Path = _DOCKER_DEFAULT_HERMES_TEMPLATES_DIR
    # Comma-separated plugin names written into every new room's config.yaml
    # under plugins.enabled. These become default tools for all containers.
    # Names must match seeded plugin directory names under HERMES_TEMPLATES_DIR/plugin/.
    DEFAULT_PLUGINS: str = "local-tools"
    # Public HTTPS base URL of this router (no trailing slash), e.g.
    # https://your-domain. Used to build the Google OAuth redirect_uri
    # ({url}/oauth/callback) and the auth links sent to LINE users. Must also
    # be added to the GCP Web application client's Authorized redirect URIs.
    GOOGLE_OAUTH_PUBLIC_URL: str = ""
    # When False, the /oauth/start and /oauth/callback routes still work, but
    # inbound LINE messages are never blocked pending Google authorization
    # (see google_oauth.check_google_authorization).
    GOOGLE_OAUTH_GATE: bool = True

    @model_validator(mode="after")
    def _validate_host_mode_paths(self) -> Settings:
        """Fail fast when host-mode dev left DATA_DIR/HERMES_TEMPLATES_DIR unset.

        Both default to container-only paths (see their field docs above)
        that don't exist on a host filesystem. Host mode must override them;
        without this check, the router starts up fine and only fails much
        later — silently, per room, the first time a room's container is
        created (see README 「設定環境變數」).

        Returns:
            Self, unchanged — this validator only raises, never mutates.

        Raises:
            ValueError: If ROUTER_IN_DOCKER is False but DATA_DIR or
                HERMES_TEMPLATES_DIR are still at their container-only defaults.
        """
        if self.ROUTER_IN_DOCKER:
            return self
        unset = [
            name
            for name, default in (
                ("DATA_DIR", _DOCKER_DEFAULT_DATA_DIR),
                ("HERMES_TEMPLATES_DIR", _DOCKER_DEFAULT_HERMES_TEMPLATES_DIR),
            )
            if getattr(self, name) == default
        ]
        if unset:
            raise ValueError(
                f"ROUTER_IN_DOCKER=false (host mode) but {', '.join(unset)} still at "
                "container-only default(s). Override in .env to this repo's absolute "
                "path — see README 「設定環境變數」."
            )
        return self

    @property
    def google_dir(self) -> Path:
        """Router-local path to the shared Google OAuth data directory.

        Returns:
            DATA_DIR / "_google" — holds tokens.json and both GCP credential
            files, shared by every room (not per-room like data/<room_id>/).
        """
        return self.DATA_DIR / "_google"

    @property
    def google_tokens_path(self) -> Path:
        """Router-local path to the shared Google OAuth tokens.json.

        Returns:
            Path to tokens.json, keyed by lowercased room id.
        """
        return self.google_dir / "tokens.json"

    @property
    def google_web_creds_path(self) -> Path:
        """Router-local path to the Web application GCP OAuth client JSON.

        Returns:
            Path to gcp-oauth.keys.json, used by the router's own oauth
            routes and by the gmail/drive MCP token refresh.
        """
        return self.google_dir / "gcp-oauth.keys.json"

    @property
    def google_installed_creds_path(self) -> Path:
        """Router-local path to the Desktop/Installed GCP OAuth client JSON.

        Returns:
            Path to gcp-oauth.keys.installed.json, used by the
            google-calendar-mcp server and scripts/google_reauth.py.
        """
        return self.google_dir / "gcp-oauth.keys.installed.json"

    @property
    def google_host_dir(self) -> Path:
        """Host filesystem path to the shared Google OAuth data directory.

        Returns:
            HOST_DATA_DIR / "_google" — the path Docker must bind-mount from
            (as opposed to google_dir, this process's own filesystem view).
        """
        return self.HOST_DATA_DIR / "_google"

    @property
    def google_oauth_enabled(self) -> bool:
        """Whether Google OAuth integration is fully configured.

        Returns:
            True when a public URL is set and the Web application
            credentials file has been placed under google_web_creds_path.
        """
        return bool(self.GOOGLE_OAUTH_PUBLIC_URL) and self.google_web_creds_path.exists()


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Returns:
        Settings: Application settings loaded from environment.
    """
    return Settings()  # type: ignore[call-arg]
