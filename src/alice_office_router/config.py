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
    # First-party API channel (TUI / mobile / dev) bearer token. Unset (None)
    # means the channel is not mounted at all (see channels.enabled_adapters).
    API_CHANNEL_TOKEN: str | None = None
    # Comma-separated call-words that address the bot in a group chat, an
    # @mention fallback for LINE clients that can't @ an OA (desktop/old
    # mobile). Empty (default) = a group message is addressed only via
    # @mention. Parsed into a tuple by group_trigger_prefixes().
    GROUP_TRIGGER_PREFIXES: str = ""
    # Per-room cap on the group observed buffer (data/<room_id>/group_state/
    # observed.jsonl): background messages beyond this are dropped oldest-first.
    GROUP_OBSERVED_MAX_MESSAGES: int = 50
    # Router-owned session-epoch rotation (see session_hygiene.py). Idle
    # threshold in minutes: when a room's previous agent turn was longer ago
    # than this, its next agent-bound message rotates to a fresh Hermes session
    # (carrying a best-effort handoff summary). <=0 disables idle rotation.
    SESSION_IDLE_RESET_MINUTES: int = 1440
    # Prompt-token watermark: when the last turn's reported prompt_tokens
    # exceeds this, the next agent-bound message rotates the session. <=0
    # disables it. IMPORTANT: usage.prompt_tokens is the SUM across all internal
    # tool-loop iterations of one request, not the context-window size — it
    # overestimates the live context and therefore fires early, which is the
    # safe direction. Do not "fix" this threshold assuming context-size
    # semantics; keep it well below Hermes's own compression trigger. Calibrated
    # against live measurement: a single simple turn in a FRESH session already
    # reports ~27k prompt_tokens in this deployment (huge Hermes system prompt +
    # skills index, summed over iterations), so a routine 2-3-iteration tool
    # turn would trip a 60k threshold with a near-empty transcript; 120000
    # leaves that headroom.
    SESSION_ROTATE_PROMPT_TOKENS: int = 120000

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
        """Router-local path to the deployment-level Google OAuth seed source.

        Returns:
            DATA_DIR / "_google" — where the operator drops both GCP client
            credential JSON files once per deployment. Never read directly by
            a room's MCPs; container_manager.ensure_google_seed copies these
            into each room's own room_google_dir the first time that room
            touches Google OAuth (see its docstring for why write-once-per-
            room, not a shared mount, is used).
        """
        return self.DATA_DIR / "_google"

    @property
    def google_web_creds_path(self) -> Path:
        """Router-local path to the deployment's Web application GCP OAuth client JSON.

        Returns:
            Path to gcp-oauth.keys.json under the seed source (google_dir),
            not any room's own copy. Used by Settings.google_oauth_enabled
            and by ensure_google_seed as the copy source.
        """
        return self.google_dir / "gcp-oauth.keys.json"

    @property
    def google_installed_creds_path(self) -> Path:
        """Router-local path to the deployment's Desktop/Installed GCP OAuth client JSON.

        Returns:
            Path to gcp-oauth.keys.installed.json under the seed source
            (google_dir), used by ensure_google_seed as the copy source.
        """
        return self.google_dir / "gcp-oauth.keys.installed.json"

    def room_google_dir(self, room_id: str) -> Path:
        """Router-local path to one room's own Google OAuth data directory.

        Args:
            room_id: Unique identifier for the chatroom, same raw (original
                case) value used for DATA_DIR / room_id elsewhere — must not
                be lowercased, or this would diverge from the directory
                container_manager actually creates for the room.

        Returns:
            DATA_DIR / room_id / "google" — holds this room's own copy of
            both GCP credential files plus this room's tokens.json. Fully
            isolated per room: deleting data/<room_id>/ wipes this room's
            Google authorization along with everything else.
        """
        return self.DATA_DIR / room_id / "google"

    def room_google_host_dir(self, room_id: str) -> Path:
        """Host filesystem path to one room's Google OAuth data directory.

        Args:
            room_id: Unique identifier for the chatroom (see room_google_dir).

        Returns:
            HOST_DATA_DIR / room_id / "google" — the path Docker must
            bind-mount from (as opposed to room_google_dir, this process's
            own filesystem view).
        """
        return self.HOST_DATA_DIR / room_id / "google"

    def room_google_tokens_path(self, room_id: str) -> Path:
        """Router-local path to one room's own Google OAuth tokens.json.

        Args:
            room_id: Unique identifier for the chatroom (see room_google_dir).

        Returns:
            Path to this room's tokens.json, keyed by its lowercased
            account_key. Never seeded — created at runtime by the OAuth
            callback or by a Google MCP's token refresh.
        """
        return self.room_google_dir(room_id) / "tokens.json"

    def room_google_web_creds_path(self, room_id: str) -> Path:
        """Router-local path to one room's own Web application GCP OAuth client JSON.

        Args:
            room_id: Unique identifier for the chatroom (see room_google_dir).

        Returns:
            Path to this room's copy of gcp-oauth.keys.json, used by the
            router's own oauth routes and by the gmail/drive MCP token
            refresh for this room.
        """
        return self.room_google_dir(room_id) / "gcp-oauth.keys.json"

    def room_google_installed_creds_path(self, room_id: str) -> Path:
        """Router-local path to one room's own Desktop/Installed GCP OAuth client JSON.

        Args:
            room_id: Unique identifier for the chatroom (see room_google_dir).

        Returns:
            Path to this room's copy of gcp-oauth.keys.installed.json, used
            by the google-calendar-mcp server for this room.
        """
        return self.room_google_dir(room_id) / "gcp-oauth.keys.installed.json"

    def room_group_state_dir(self, room_id: str) -> Path:
        """Router-local path to one room's group-chat observed-buffer directory.

        Args:
            room_id: Unique identifier for the chatroom, same raw (original
                case) value used for DATA_DIR / room_id elsewhere — must not
                be lowercased, or this would diverge from the directory
                container_manager actually creates for the room.

        Returns:
            DATA_DIR / room_id / "group_state" — holds this room's
            observed.jsonl background buffer. Lives inside the room's own
            /opt/data mount but is named so it never collides with anything
            Hermes gateway manages, so Hermes leaves it alone.
        """
        return self.DATA_DIR / room_id / "group_state"

    def room_router_state_dir(self, room_id: str) -> Path:
        """Router-local path to one room's session-hygiene state directory.

        Args:
            room_id: Unique identifier for the chatroom, same raw (original
                case) value used for DATA_DIR / room_id elsewhere — must not
                be lowercased, or this would diverge from the directory
                container_manager actually creates for the room.

        Returns:
            DATA_DIR / room_id / "router_state" — holds this room's session.json
            (the epoch, activity/token watermarks, and any pending handoff; see
            session_hygiene.py). Lives inside the room's own /opt/data mount but
            is named so it never collides with anything Hermes gateway manages,
            so Hermes leaves it alone.
        """
        return self.DATA_DIR / room_id / "router_state"

    def group_trigger_prefixes(self) -> tuple[str, ...]:
        """Parse GROUP_TRIGGER_PREFIXES into the non-empty call-words to match.

        Returns:
            The comma-separated prefixes, each stripped, with blanks dropped.
            An empty tuple (the default) means a group message is addressed
            only by an @mention, never by a leading call-word.
        """
        return tuple(
            part.strip() for part in self.GROUP_TRIGGER_PREFIXES.split(",") if part.strip()
        )

    @property
    def google_oauth_enabled(self) -> bool:
        """Whether Google OAuth integration is fully configured for this deployment.

        Returns:
            True when a public URL is set and the Web application
            credentials file has been placed under google_web_creds_path
            (the deployment-level seed source, not any room's own copy).
        """
        return bool(self.GOOGLE_OAUTH_PUBLIC_URL) and self.google_web_creds_path.exists()


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Returns:
        Settings: Application settings loaded from environment.
    """
    return Settings()  # type: ignore[call-arg]
