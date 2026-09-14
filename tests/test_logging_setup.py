from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
from structlog.contextvars import bound_contextvars

from alice_office_router.config import Settings
from alice_office_router.logging_setup import RequestContextMiddleware, configure_logging


def _settings(**overrides: str) -> Settings:
    return Settings(
        LINE_CHANNEL_SECRET="test_secret",
        LINE_CHANNEL_ACCESS_TOKEN="test_token",
        HERMES_API_SERVER_KEY="test_api_server_key",
        **overrides,
    )


def _capture(settings: Settings) -> io.StringIO:
    """Configure logging for these settings, then divert its handler to a buffer."""
    configure_logging(settings)
    stream = io.StringIO()
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(stream)
    return stream


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Put the real stdout handler (and an empty context) back after each test."""
    yield
    structlog.contextvars.clear_contextvars()
    configure_logging(_settings())


# ---------------------------------------------------------------------------
# configure_logging — rendering


def test_json_format_emits_one_parsable_object_per_line() -> None:
    stream = _capture(_settings())

    logging.getLogger("alice_office_router.demo").info("hello")

    records = _records(stream)
    assert len(records) == 1
    assert records[0]["event"] == "hello"
    assert records[0]["level"] == "info"
    assert records[0]["logger"] == "alice_office_router.demo"
    assert str(records[0]["ts"]).endswith("Z")


def test_bound_contextvars_land_on_every_line() -> None:
    stream = _capture(_settings())

    with bound_contextvars(room_key="line_U1", channel="line"):
        logging.getLogger("alice_office_router.demo").info("in room")
    logging.getLogger("alice_office_router.demo").info("out of room")

    inside, outside = _records(stream)
    assert inside["room_key"] == "line_U1"
    assert inside["channel"] == "line"
    assert "room_key" not in outside


def test_exception_is_rendered_as_a_string() -> None:
    stream = _capture(_settings())

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("alice_office_router.demo").error("failed", exc_info=True)

    rendered = _records(stream)[0]["exception"]
    assert isinstance(rendered, str)
    assert "ValueError: boom" in rendered


def test_log_level_debug_enables_debug_records() -> None:
    stream = _capture(_settings(LOG_LEVEL="DEBUG"))

    logging.getLogger("alice_office_router.demo").debug("chatty")

    assert [r["level"] for r in _records(stream)] == ["debug"]


def test_default_level_drops_debug_records() -> None:
    stream = _capture(_settings())

    logging.getLogger("alice_office_router.demo").debug("chatty")

    assert _records(stream) == []


def test_noisy_third_party_loggers_stay_at_warning() -> None:
    stream = _capture(_settings(LOG_LEVEL="DEBUG"))

    logging.getLogger("docker.api.client").info("docker noise")
    logging.getLogger("httpx").info("httpx noise")
    logging.getLogger("alice_office_router.demo").info("kept")

    assert [r["event"] for r in _records(stream)] == ["kept"]


def test_console_format_renders_plain_text_not_json() -> None:
    stream = _capture(_settings(LOG_FORMAT="console"))

    logging.getLogger("alice_office_router.demo").info("console line")

    output = stream.getvalue()
    assert "console line" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


def test_configure_logging_is_idempotent() -> None:
    settings = _settings()
    configure_logging(settings)
    configure_logging(settings)
    stream = _capture(settings)

    logging.getLogger("alice_office_router.demo").info("once")

    assert len(_records(stream)) == 1
    assert len(logging.getLogger().handlers) == 1


def test_uvicorn_access_logger_has_no_handler_of_its_own() -> None:
    configure_logging(_settings())

    access_logger = logging.getLogger("uvicorn.access")
    assert access_logger.handlers == []
    assert access_logger.propagate is False


# ---------------------------------------------------------------------------
# RequestContextMiddleware — request_id + the one access line


async def test_request_emits_one_access_line_with_request_id(client: AsyncClient) -> None:
    stream = _capture(_settings())

    response = await client.get("/no-such-route")

    assert response.status_code == 404
    access = [r for r in _records(stream) if r["event"] == "http_request"]
    assert len(access) == 1
    assert access[0]["method"] == "GET"
    assert access[0]["path"] == "/no-such-route"
    assert access[0]["status"] == 404
    assert isinstance(access[0]["duration_ms"], float)
    assert len(str(access[0]["request_id"])) == 32


async def test_each_request_gets_its_own_request_id(client: AsyncClient) -> None:
    stream = _capture(_settings())

    await client.get("/no-such-route")
    await client.get("/no-such-route-either")

    request_ids = {r["request_id"] for r in _records(stream) if r["event"] == "http_request"}
    assert len(request_ids) == 2


async def _call(app: object, method: str, path: str) -> None:
    """Drive one request through a middleware-wrapped ASGI app."""
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.request(method, path)


async def test_access_line_is_emitted_before_background_tasks_run() -> None:
    """A LINE webhook answers 200 and *then* runs a 90s agent turn in a BackgroundTask.

    The access line must describe the 200 the caller waited for, so it has to be
    out before that task starts — not after it in a `finally`.
    """
    stream = _capture(_settings())

    def agent_turn() -> None:
        logging.getLogger("alice_office_router.demo").info("background_turn")

    async def endpoint(request: Request) -> Response:
        return Response("ok", background=BackgroundTask(agent_turn))

    app = RequestContextMiddleware(
        Starlette(routes=[Route("/webhooks/fake", endpoint, methods=["POST"])])
    )

    await _call(app, "POST", "/webhooks/fake")

    assert [r["event"] for r in _records(stream)] == ["http_request", "background_turn"]


async def test_access_line_reports_the_response_status_once() -> None:
    """The send-wrapper path still reports the real status, exactly one line per request."""
    stream = _capture(_settings())

    async def endpoint(request: Request) -> Response:
        return Response("nope", status_code=418)

    app = RequestContextMiddleware(Starlette(routes=[Route("/teapot", endpoint)]))

    await _call(app, "GET", "/teapot")

    access = [r for r in _records(stream) if r["event"] == "http_request"]
    assert len(access) == 1
    assert access[0]["status"] == 418


async def test_access_line_reports_5xx_when_the_app_raises() -> None:
    """No response ever starts, so the `finally` fallback logs the 500 the caller sees."""
    stream = _capture(_settings())

    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    app = RequestContextMiddleware(broken)

    with pytest.raises(RuntimeError):
        await _call(app, "GET", "/boom")

    access = [r for r in _records(stream) if r["event"] == "http_request"]
    assert len(access) == 1
    assert access[0]["status"] == 500
    assert access[0]["path"] == "/boom"
