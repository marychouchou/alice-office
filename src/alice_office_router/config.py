from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Container-only defaults for DATA_DIR/HERMES_TEMPLATES_DIR (see their field
# docs below). Host-mode dev must override both — see _validate_host_mode_paths.
_DOCKER_DEFAULT_DATA_DIR = Path("/app/data")
_DOCKER_DEFAULT_HERMES_TEMPLATES_DIR = Path("/app/hermes-templates")

# The stdlib logging level names, which is exactly what dictConfig accepts.
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    # extra="ignore": .env also carries compose-only variables the router never
    # reads (GRAFANA_ADMIN_PASSWORD, SEARXNG_SECRET — see .env.example). With
    # pydantic-settings' default "forbid", any such key made every request 500
    # with "Extra inputs are not permitted".
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

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
    # Maximum SILENCE (seconds) between bytes of one agent turn before the agent
    # is considered dead. The router asks for a streamed response, and Hermes
    # writes a `: keepalive` comment after every 30s of inactivity — including
    # while a tool runs — so liveness is "bytes keep arriving", not "the turn
    # finished in time". This is what a hung container trips, usually within
    # minutes; a legitimately slow turn never does, however long it thinks.
    HERMES_IDLE_TIMEOUT_SECONDS: float = 120.0
    # Absolute ceiling (seconds) on ONE agent turn, a safety valve for a stream
    # that keeps emitting keepalives forever. Normally never reached: liveness
    # is judged by HERMES_IDLE_TIMEOUT_SECONDS above, and a turn is a whole tool
    # loop, so pure-reasoning or many-tool turns legitimately run for many
    # minutes. The webhook itself already returned 200 and the turn runs in a
    # background task, so raising this does not affect LINE's own webhook
    # deadline. On expiry (either budget) the router stops waiting and sends the
    # user the fixed timeout notice (core.AGENT_TIMEOUT_NOTICE) — the agent is
    # not interrupted, so that turn's answer still lands in the room's Hermes
    # session.
    HERMES_REQUEST_TIMEOUT_SECONDS: float = 3600.0
    # Set False when router runs on the host (not inside Docker).
    # Containers will publish port 8642 to a random host port so the host
    # can reach them via localhost instead of Docker-internal DNS.
    ROUTER_IN_DOCKER: bool = True
    # LLM endpoint forwarded into every Hermes agent container.
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = ""
    # Self-hosted SearXNG base URL forwarded into every Hermes agent container
    # as SEARXNG_URL (deploy/searxng/). Hermes's built-in web_search tool only
    # appears in the agent's tool list once some search provider is available,
    # and auto-selects SearXNG when this env var is set — no per-room
    # config.yaml change needed. Empty (default) = web_search stays hidden,
    # exactly as before. Containers read env only at creation, so existing
    # rooms need `docker rm -f hermes_<room_id>` to pick it up.
    SEARXNG_URL: str = ""
    # Router-local path (like DATA_DIR — the router's own filesystem view,
    # NOT a host path for Docker volume mounting) to this repo's src/hermes/
    # directory. Holds mcp/<name>/ and plugin/<name>/ source templates, and
    # SOUL.md (the agent's persona) — room_seed.ensure_mcp_seed /
    # ensure_plugin_seed / ensure_soul_seed copy each one into a room's data
    # dir (data/<room_id>/mcp/<name>/, data/<room_id>/plugins/<name>/,
    # data/<room_id>/SOUL.md) the first time that room's container is
    # created, then never touch it again — the room's copy is the room's own
    # to edit from then on. Also holds config.template.yml (see
    # container_manager._ensure_config_yaml), which unlike those is a
    # str.format() template rather than something copied verbatim.
    # In Docker mode, docker-compose.yml mounts ./src/hermes here read-only.
    # Host-dev mode must point this at the repo's actual src/hermes path.
    HERMES_TEMPLATES_DIR: Path = _DOCKER_DEFAULT_HERMES_TEMPLATES_DIR
    # Comma-separated plugin names written into every new room's config.yaml
    # under plugins.enabled. These become default tools for all containers.
    # Names must match seeded plugin directory names under HERMES_TEMPLATES_DIR/plugin/.
    DEFAULT_PLUGINS: str = "local-tools"
    # Public HTTPS base URL of this router (no trailing slash), e.g.
    # https://your-domain — the one address users' browsers reach it at
    # (typically a Cloudflare tunnel). Used to build the Google OAuth
    # redirect_uri ({url}/oauth/callback, which must also be registered in the
    # GCP Web application client's Authorized redirect URIs) and the auth links
    # sent to LINE users. Renamed from GOOGLE_OAUTH_PUBLIC_URL on 2026-09-17;
    # the old name is not read.
    PUBLIC_BASE_URL: str = ""
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
    # semantics; keep it well ABOVE Hermes's own compression trigger (floored
    # at 75% of the LLM backend's context window) so compression gets a chance
    # to fire first. Calibrated against live measurement: a single simple turn
    # in a FRESH session already reports ~27k prompt_tokens in this deployment
    # (huge Hermes system prompt + skills index, summed over iterations), so a
    # routine 2-3-iteration tool turn would trip a 60k threshold with a
    # near-empty transcript. 2026-09-15: LLM backend window doubled to 262144
    # (compression trigger ~197k), so this doubled in lockstep to 240000.
    SESSION_ROTATE_PROMPT_TOKENS: int = 240000
    # Minimum level every logger in the router process emits (see
    # logging_setup.configure_logging). Noisy third-party loggers (docker,
    # httpx, ...) stay pinned at WARNING regardless, so DEBUG stays readable.
    # Typed as a Literal so a typo fails at startup with a pydantic error
    # naming the field and the five valid values — rather than reaching
    # dictConfig, which raises a ValueError about an "unknown level" from deep
    # inside logging.config with no mention of which setting caused it.
    LOG_LEVEL: LogLevel = "INFO"
    # Rendering of those log lines: "json" (default — one JSON object per
    # line, what a collector reads) or "console" (colored, human-readable;
    # for host-mode dev in a terminal).
    LOG_FORMAT: Literal["json", "console"] = "json"
    # Whether each inbound turn also appends one JSON envelope line to
    # DATA_DIR/_conversations/<room_key>.jsonl (see conversation_log.py). The
    # envelope never carries the agent's reply text — conversation content lives
    # only in each room's Hermes state.db (docs/logging-design.md §5.7). Set
    # False for a deployment contractually barred from keeping any per-turn
    # record; the same line still goes to stdout for the log collector.
    CONVERSATION_LOG_ENABLED: bool = True

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        """Accept `LOG_LEVEL=debug` as well as the canonical upper-case name.

        Args:
            value: The raw environment value, before the Literal is checked.

        Returns:
            The string upper-cased; anything that is not a string is passed
            through untouched, so pydantic reports the type error itself.
        """
        return value.upper() if isinstance(value, str) else value

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
            a room's MCPs; room_seed.ensure_google_seed copies these
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

    @property
    def conversations_dir(self) -> Path:
        """Router-local path to the cross-room turn-envelope directory.

        Returns:
            DATA_DIR / "_conversations" — one <room_key>.jsonl per room, each
            line a TurnEnvelope (conversation_log.py). Underscore-prefixed like
            google_dir so it never collides with a room directory, and so the
            conversations CLI can skip it when enumerating rooms.
        """
        return self.DATA_DIR / "_conversations"

    def room_conversation_log(self, room_id: str) -> Path:
        """Router-local path to one room's turn-envelope JSONL file.

        Args:
            room_id: Unique identifier for the chatroom, same raw (original
                case) value used for DATA_DIR / room_id elsewhere — must not
                be lowercased, or the CLI's join back onto the room's
                data/<room_id>/state.db would miss.

        Returns:
            DATA_DIR / "_conversations" / f"{room_id}.jsonl" — deliberately
            OUTSIDE data/<room_id>/ (which is bind-mounted into the room's
            container as /opt/data), so a room's own agent can never read or
            rewrite the router's record of that room.
        """
        return self.conversations_dir / f"{room_id}.jsonl"

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
        return bool(self.PUBLIC_BASE_URL) and self.google_web_creds_path.exists()


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Returns:
        Settings: Application settings loaded from environment.
    """
    return Settings()  # type: ignore[call-arg]
