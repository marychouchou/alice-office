"""Structured logging setup: one renderer for every logger in the process.

Replaces the old `logging.basicConfig` call. `configure_logging` routes this
package's own `logging.getLogger(__name__)` loggers, structlog loggers, and
uvicorn's loggers through a single `structlog.stdlib.ProcessorFormatter`, so
every line on stdout has the same shape — JSON in production (one object per
line, `| jq .` clean) or a colored console rendering during host-mode dev.
`RequestContextMiddleware` binds one `request_id` per HTTP request into
`structlog.contextvars`, so every line emitted while serving that request
carries it without any call site having to pass it along; the same mechanism
carries `room_key`, `event_id`, `channel` and `container`, bound at the points
listed in docs/logging-design.md §5.1.
"""

from __future__ import annotations

import logging
import logging.config
import time
from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import Processor

from alice_office_router.config import Settings

# Third-party loggers that are chatty at DEBUG/INFO and say nothing about this
# application's behavior; pinned to WARNING so LOG_LEVEL=DEBUG stays readable.
_NOISY_LOGGERS = ("docker", "urllib3", "httpx", "httpcore")

# ISO-8601 UTC, under `ts` rather than structlog's default `timestamp` key —
# short, and the name the Loki/Alloy pipeline in docs/logging-design.md §4 uses.
_TIMESTAMPER = structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts")

# The access log line is emitted by the middleware below, not by uvicorn (whose
# own access logger is left handler-less by configure_logging).
_access_logger = structlog.stdlib.get_logger("alice_office_router.access")


def _render_processors(log_format: str) -> list[Processor]:
    """Build the tail of the chain that turns one event dict into one line.

    Args:
        log_format: Settings.LOG_FORMAT — "console" for the human-readable
            development rendering, anything else for the JSON production one.

    Returns:
        Processors for ProcessorFormatter, ending in a renderer. Exceptions
        are rendered to a string under `exception` for JSON; ConsoleRenderer
        formats them itself, so format_exc_info is deliberately absent there.
    """
    if log_format == "console":
        return [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(timestamp_key="ts"),
        ]
    return [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.format_exc_info,
        # ensure_ascii=False keeps Chinese log text readable; default=str keeps
        # a stray Path/exception value from turning a log call into a crash.
        structlog.processors.JSONRenderer(ensure_ascii=False, default=str),
    ]


def configure_logging(config: Settings) -> None:
    """Point every logger in this process at one structlog renderer.

    Idempotent — calling it again fully replaces the previous configuration,
    so tests (and a reload-mode restart) may call it repeatedly. uvicorn's own
    access logger is left with no handlers and propagate=False, because
    RequestContextMiddleware emits the structured `http_request` line instead;
    leaving both on would put two formats in the same stdout stream.

    Args:
        config: Application settings; LOG_LEVEL sets the level of this app's
            and uvicorn's loggers, LOG_FORMAT picks the renderer.
    """
    # Settings already validated this against the stdlib level names (and
    # upper-cased it), so dictConfig can never see an unknown level here.
    level = config.LOG_LEVEL
    # Runs for records that did NOT come from structlog (stdlib loggers, and
    # uvicorn's), giving them the same fields a structlog event dict carries.
    foreign_pre_chain: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        _TIMESTAMPER,
    ]
    dict_config: dict[str, object] = {
        "version": 1,
        # Module-level loggers are created at import time, before this runs.
        "disable_existing_loggers": False,
        "formatters": {
            "structured": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processors": _render_processors(config.LOG_FORMAT),
                "foreign_pre_chain": foreign_pre_chain,
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "structured",
            },
        },
        "loggers": {
            "": {"handlers": ["default"], "level": level},
            "uvicorn": {"handlers": ["default"], "level": level, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": level, "propagate": False},
            "uvicorn.access": {"handlers": [], "level": level, "propagate": False},
            **{name: {"level": "WARNING"} for name in _NOISY_LOGGERS},
        },
    }
    logging.config.dictConfig(dict_config)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.PositionalArgumentsFormatter(),
            _TIMESTAMPER,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # False so a re-configuration (tests, reload) actually takes effect on
        # loggers that were already obtained at import time.
        cache_logger_on_first_use=False,
    )


class RequestContextMiddleware:
    """Pure-ASGI middleware: one request id per request, one access line.

    Pure ASGI rather than BaseHTTPMiddleware so the request context is bound in
    the same task that later runs FastAPI's background tasks, and so nothing
    buffers the response body.

    The line is emitted from the `send` wrapper, the moment the response body
    is complete — deliberately NOT from a `finally` around `self.app(...)`.
    Starlette runs a response's BackgroundTasks after the response is sent but
    still inside that call, so a LINE webhook's 90-second agent turn would land
    between the request and its own access line: the line would arrive last,
    and its `duration_ms` would measure the agent turn rather than the 200 the
    caller actually waited for. The `finally` block is kept only as the
    fallback for a request that never produced a response at all (the app
    raised), so every request still logs exactly one line.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Store the next ASGI application in the chain.

        Args:
            app: The ASGI application this middleware wraps.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the request context, run the app, and log one access line.

        Args:
            scope: ASGI connection scope; non-HTTP scopes (lifespan, websocket)
                pass straight through untouched.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=uuid4().hex)
        started = time.perf_counter()
        status = 500
        logged = False

        def emit_access_line() -> None:
            """Log this request's one access line; later calls are no-ops."""
            nonlocal logged
            if logged:
                return
            logged = True
            _access_logger.info(
                "http_request",
                method=str(scope.get("method", "")),
                path=str(scope.get("path", "")),
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)
            # The final body chunk means the client has the whole response;
            # anything after this (BackgroundTasks) is no longer this request's
            # latency, so the line goes out here rather than in the `finally`.
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                emit_access_line()

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Only reached without a line when no response completed — the app
            # raised, and `status` is still the 500 the caller will see.
            emit_access_line()
