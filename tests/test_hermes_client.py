from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from unittest.mock import Mock, patch

import httpx
import pytest

from alice_office_router.hermes_client import AgentReply, ask_hermes_agent

Handler = Callable[[httpx.Request], httpx.Response]

# Every test that does not care about the budgets uses these: far larger than
# any mocked stream needs, so only the tests that deliberately starve the
# stream ever see a timeout.
_IDLE = 5.0
_MAX = 30.0


def _chunk(**fields: object) -> str:
    """Render one SSE data line carrying a chat completion chunk.

    Args:
        **fields: Top-level chunk fields (choices, usage, hermes, error).

    Returns:
        A `data: {...}` line, without its terminator.
    """
    return "data: " + json.dumps({"object": "chat.completion.chunk", **fields})


def _content_chunk(text: str) -> str:
    """Render a content delta chunk carrying one fragment of the reply."""
    return _chunk(choices=[{"index": 0, "delta": {"content": text}, "finish_reason": None}])


def _finish_chunk(reason: str = "stop", **fields: object) -> str:
    """Render the finish chunk that closes a streamed turn.

    Args:
        reason: The finish_reason to report ("stop", "length", ...).
        **fields: Extra top-level fields (usage, hermes, error).

    Returns:
        A `data: {...}` line, without its terminator.
    """
    return _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": reason}], **fields)


def _body(*lines: str) -> bytes:
    """Frame SSE lines into a response body, blank line after each event."""
    return "".join(f"{line}\n\n" for line in lines).encode()


_REPLY_BODY = _body(
    _chunk(choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]),
    _content_chunk("哈囉，"),
    _content_chunk("我是 Hermes"),
    _finish_chunk(usage={"prompt_tokens": 1234, "completion_tokens": 7, "total_tokens": 1241}),
    "data: [DONE]",
)


@contextmanager
def _mock_transport(handler: Handler) -> Iterator[list[httpx.Request]]:
    """Serve every AsyncClient request from `handler`, recording the requests.

    ask_hermes_agent builds its own client, so the seam is the client factory:
    the real class is called with a MockTransport injected, which leaves the
    streaming, timeout and SSE decoding machinery genuinely exercised.

    Args:
        handler: Called with each request, returns the response to serve.

    Yields:
        The list of requests seen so far.
    """
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(_record), **kwargs)  # type: ignore[arg-type]

    with patch.object(httpx, "AsyncClient", _factory):
        yield seen


@contextmanager
def _serving(body: bytes, status_code: int = 200) -> Iterator[list[httpx.Request]]:
    """Serve one fixed SSE body for every request.

    Args:
        body: The raw response body to stream back.
        status_code: The HTTP status to report.

    Yields:
        The list of requests seen so far.
    """
    with _mock_transport(
        lambda _request: httpx.Response(
            status_code, headers={"content-type": "text/event-stream"}, content=body
        )
    ) as seen:
        yield seen


async def _ask(
    *,
    system: str | None = None,
    idle_timeout_seconds: float = _IDLE,
    max_seconds: float = _MAX,
) -> AgentReply:
    """Call ask_hermes_agent with the standard test arguments.

    Args:
        system: Optional ephemeral system message for the turn.
        idle_timeout_seconds: Silence budget, generous unless a test starves it.
        max_seconds: Absolute ceiling, generous unless a test shrinks it.

    Returns:
        Whatever the client assembled from the mocked stream.
    """
    return await ask_hermes_agent(
        "http://hermes_room_AAA:8642",
        "room_AAA",
        "哈囉",
        "test_key",
        idle_timeout_seconds=idle_timeout_seconds,
        max_seconds=max_seconds,
        system=system,
    )


# ---------------------------------------------------------------------------
# Reply assembly
# ---------------------------------------------------------------------------


async def test_ask_hermes_agent_assembles_text_across_content_chunks() -> None:
    """The reply is the concatenation of every delta.content, role-only chunk aside."""
    with _serving(_REPLY_BODY):
        reply = await _ask()

    assert reply.text == "哈囉，我是 Hermes"


async def test_ask_hermes_agent_ignores_keepalives_and_blank_lines() -> None:
    """`: keepalive` comments (the liveness signal) carry no payload."""
    body = (
        b": keepalive\n\n"
        + _body(_content_chunk("等一下"))
        + b"\n: keepalive\n\n"
        + _body(_content_chunk("好了"), _finish_chunk(), "data: [DONE]")
    )
    with _serving(body):
        reply = await _ask()

    assert reply.text == "等一下好了"


async def test_ask_hermes_agent_takes_prompt_tokens_from_the_finish_chunk() -> None:
    """usage.prompt_tokens rides the finish chunk and reaches the AgentReply."""
    with _serving(_REPLY_BODY):
        reply = await _ask()

    assert reply.prompt_tokens == 1234


async def test_ask_hermes_agent_missing_usage_yields_none_prompt_tokens() -> None:
    """No usage at all leaves prompt_tokens None (nothing to watermark)."""
    with _serving(_body(_content_chunk("hi"), _finish_chunk(), "data: [DONE]")):
        reply = await _ask()

    assert reply.prompt_tokens is None


@pytest.mark.parametrize("reported", [0, -5])
async def test_ask_hermes_agent_normalizes_nonpositive_prompt_tokens_to_none(
    reported: int,
) -> None:
    """A reported 0/negative is the server's "didn't count" default -> None."""
    body = _body(
        _content_chunk("hi"),
        _finish_chunk(usage={"prompt_tokens": reported}),
        "data: [DONE]",
    )
    with _serving(body):
        reply = await _ask()

    assert reply.prompt_tokens is None


async def test_ask_hermes_agent_stops_at_the_done_sentinel() -> None:
    """Anything after `[DONE]` is not part of the turn."""
    body = _body(_content_chunk("hi"), _finish_chunk(), "data: [DONE]", _content_chunk("ignored"))
    with _serving(body):
        reply = await _ask()

    assert reply.text == "hi"


async def test_ask_hermes_agent_skips_a_data_line_that_will_not_parse() -> None:
    """One corrupt frame must not cost the user the rest of the turn."""
    body = _body(
        _content_chunk("前半"),
        'data: {"choices": "not-an-array"}',
        _content_chunk("後半"),
        _finish_chunk(),
        "data: [DONE]",
    )
    with _serving(body), patch("alice_office_router.hermes_client.logger", new=Mock()) as log:
        reply = await _ask()

    assert reply.text == "前半後半"
    assert log.warning.call_args[0] == ("hermes_agent_chunk_skipped",)


# ---------------------------------------------------------------------------
# Outcome rules
# ---------------------------------------------------------------------------


async def test_ask_hermes_agent_returns_truncated_text_with_a_warning() -> None:
    """finish_reason "length": half an answer beats none, but it is logged."""
    body = _body(
        _content_chunk("開頭講到一半"),
        _finish_chunk(
            "length",
            hermes={"completed": False, "partial": True, "failed": False, "error": None},
        ),
        "data: [DONE]",
    )
    with _serving(body), patch("alice_office_router.hermes_client.logger", new=Mock()) as log:
        reply = await _ask()

    assert reply.text == "開頭講到一半"
    assert log.warning.call_args[0] == ("hermes_agent_truncated",)
    assert log.warning.call_args[1]["finish_reason"] == "length"


async def test_ask_hermes_agent_raises_when_hermes_reports_a_failed_turn() -> None:
    """A `hermes.failed` finish chunk is an agent failure, not a reply."""
    body = _body(
        _content_chunk("部分內容"),
        _finish_chunk(
            "error",
            error={"message": "tool crashed", "type": "agent_error"},
            hermes={"failed": True, "error": "tool crashed", "error_code": "agent_error"},
        ),
        "data: [DONE]",
    )
    with _serving(body), pytest.raises(ValueError, match="Hermes agent failed: tool crashed"):
        await _ask()


async def test_ask_hermes_agent_raises_on_a_stream_with_no_content() -> None:
    """A stream that finished without any text has no usable reply."""
    body = _body(_finish_chunk(), "data: [DONE]")
    with _serving(body), pytest.raises(ValueError, match="no content"):
        await _ask()


async def test_ask_hermes_agent_raises_on_http_error() -> None:
    """A non-2xx response propagates as an httpx error."""
    with _serving(b'{"detail": "boom"}', status_code=500), pytest.raises(httpx.HTTPStatusError):
        await _ask()


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


async def test_ask_hermes_agent_requests_a_stream_with_auth_and_session_headers() -> None:
    """The request opts into SSE and carries the Bearer token + session id."""
    with _serving(_REPLY_BODY) as seen:
        await _ask()

    request = seen[0]
    # httpx lower-cases the host; only the path is ours to assert exactly.
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test_key"
    assert request.headers["X-Hermes-Session-Id"] == "room_AAA"
    body = json.loads(request.content)
    assert body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "哈囉"}]


async def test_ask_hermes_agent_prepends_system_message_when_given() -> None:
    """A `system` argument is sent as a leading system message before the user turn."""
    with _serving(_REPLY_BODY) as seen:
        await _ask(system="be brief")

    assert json.loads(seen[0].content)["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "哈囉"},
    ]


# ---------------------------------------------------------------------------
# The two budgets
# ---------------------------------------------------------------------------


async def test_ask_hermes_agent_propagates_the_idle_read_timeout() -> None:
    """Silence longer than idle_timeout_seconds surfaces as httpx.ReadTimeout."""

    def _silent(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    with _mock_transport(_silent), pytest.raises(httpx.ReadTimeout):
        await _ask(idle_timeout_seconds=0.05)


async def test_ask_hermes_agent_raises_timeout_error_at_the_ceiling() -> None:
    """A stream that keeps trickling forever is cut off by max_seconds."""

    async def _endless() -> AsyncIterator[bytes]:
        yield b": keepalive\n\n"
        await asyncio.sleep(30)
        yield b"data: [DONE]\n\n"

    def _trickle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_endless()
        )

    with _mock_transport(_trickle), pytest.raises(TimeoutError):
        await asyncio.wait_for(_ask(max_seconds=0.05), timeout=5)
