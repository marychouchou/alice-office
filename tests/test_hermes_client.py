from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from unittest.mock import Mock, patch

import httpx
import pytest

from alice_office_router.hermes_client import (
    AgentReply,
    ask_hermes_agent,
    delete_hermes_session,
)

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


def _tool_progress_frame(**fields: object) -> str:
    """Render Hermes's tool-start SSE frame: a named event, not a bare data line.

    This is the only round boundary the wire format exposes
    (docs/router-hermes-agent-protocol.md); ask_hermes_agent uses it to drop
    narration produced before a tool call.
    """
    return "event: hermes.tool.progress\ndata: " + json.dumps(
        fields or {"tool": "browser_navigate"}
    )


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


def _session_stats_not_found(_request: httpx.Request) -> httpx.Response:
    """Default GET /api/sessions/{id} response: the session does not exist yet.

    Every ask_hermes_agent call now brackets its POST with two of these GETs
    (docs/router-hermes-agent-protocol.md), so any test not specifically about
    tool_calls/api_calls needs a harmless default for them: a 404 on both
    reads resolves to a clean (0, 0) -> (0, 0) delta instead of the
    `hermes_session_stats_unavailable` warning an unparseable body would log.
    """
    return httpx.Response(404, json={"error": {"message": "not found"}})


def _session_stats_ok(*, tool_call_count: int, api_call_count: int) -> httpx.Response:
    """Render a `GET /api/sessions/{id}` 200 body with the given counters."""
    return httpx.Response(
        200,
        json={
            "object": "hermes.session",
            "session": {
                "tool_call_count": tool_call_count,
                "api_call_count": api_call_count,
            },
        },
    )


def _only_post(seen: list[httpx.Request]) -> httpx.Request:
    """Return the turn's one POST /v1/chat/completions request out of `seen`.

    Every call also makes two GET /api/sessions/{id} bracket requests now, so
    a test asserting on "the request" must pick the POST out explicitly.

    Args:
        seen: All requests recorded by a `_mock_transport` handler.

    Returns:
        The single POST request among them.
    """
    posts = [r for r in seen if r.method == "POST"]
    assert len(posts) == 1
    return posts[0]


@contextmanager
def _serving(body: bytes, status_code: int = 200) -> Iterator[list[httpx.Request]]:
    """Serve one fixed SSE body for the turn's POST; 404 its stats GETs.

    Args:
        body: The raw response body to stream back for the POST call.
        status_code: The HTTP status to report for the POST call.

    Yields:
        The list of requests seen so far.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _session_stats_not_found(request)
        return httpx.Response(
            status_code, headers={"content-type": "text/event-stream"}, content=body
        )

    with _mock_transport(_handler) as seen:
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


async def test_ask_hermes_agent_drops_narration_from_earlier_tool_rounds() -> None:
    """Only content after the *last* tool_progress event reaches the reply.

    Hermes streams a model's "let me try this" commentary before every tool
    call as ordinary content chunks, indistinguishable on the wire from its
    real final answer — the named `hermes.tool.progress` event is the only
    boundary marking one round from the next.
    """
    body = _body(
        _content_chunk("讓我先查一下天氣"),
        _tool_progress_frame(),
        _content_chunk("這個網址不行，換一個："),
        _tool_progress_frame(),
        _content_chunk("查到了，台北 27 度"),
        _finish_chunk(),
        "data: [DONE]",
    )
    with _serving(body):
        reply = await _ask()

    assert reply.text == "查到了，台北 27 度"


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
    """A stream that finished without any text has no usable reply.

    The GET /api/sessions/{id}/messages fallback also 404s here (`_serving`
    404s every GET), so this doubles as "the fallback read itself failed"
    coverage; test_ask_hermes_agent_raises_when_history_fallback_has_no_text
    below covers "the fallback succeeded but found nothing usable" instead.
    """
    body = _body(_finish_chunk(), "data: [DONE]")
    with _serving(body), pytest.raises(ValueError, match="no content"):
        await _ask()


def _messages_handler(
    body: bytes, history: list[dict[str, object]]
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a handler serving `body` for the POST and `history` as the GET .../messages data.

    Args:
        body: SSE body to stream back for the turn's POST.
        history: Rows for the `GET /api/sessions/{id}/messages` response's
            `data` array (the session-history fallback ask_hermes_agent reads
            when the stream itself carries no content).

    Returns:
        A handler for _mock_transport: routes the POST to `body`, a GET whose
        path ends in "/messages" to `history`, and any other GET (the
        tool/api call-count brackets) to the "session not found yet" 404.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200, json={"object": "list", "session_id": "room_AAA", "data": history}
            )
        return _session_stats_not_found(request)

    return _handler


async def test_ask_hermes_agent_recovers_text_from_session_history_when_the_stream_is_empty() -> (
    None
):
    """Hermes's own max-iterations summary never reaches the SSE stream; state.db still has it."""
    body = _body(_finish_chunk(), "data: [DONE]")
    history = [
        {"role": "user", "content": "問題"},
        # An assistant message mid-tool-call has empty content — must be
        # skipped in favor of the later one that actually has text.
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        {"role": "tool", "content": "工具結果"},
        {"role": "assistant", "content": "從歷史救回的答案"},
    ]
    with (
        _mock_transport(_messages_handler(body, history)),
        patch("alice_office_router.hermes_client.logger", new=Mock()) as log,
    ):
        reply = await _ask()

    assert reply.text == "從歷史救回的答案"
    assert log.warning.call_args[0] == ("hermes_agent_stream_empty_recovered_from_history",)
    assert log.warning.call_args[1]["finish_reason"] == "stop"


async def test_ask_hermes_agent_raises_when_history_fallback_has_no_text() -> None:
    """The stream is empty and the fallback's history has no non-empty assistant message either."""
    body = _body(_finish_chunk(), "data: [DONE]")
    history = [
        {"role": "user", "content": "問題"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
    ]
    with (
        _mock_transport(_messages_handler(body, history)),
        pytest.raises(ValueError, match="no content"),
    ):
        await _ask()


async def test_ask_hermes_agent_raises_on_http_error() -> None:
    """A non-2xx response propagates as an httpx error."""
    with _serving(b'{"detail": "boom"}', status_code=500), pytest.raises(httpx.HTTPStatusError):
        await _ask()


# ---------------------------------------------------------------------------
# Session call counts (docs/router-hermes-agent-protocol.md, docs/logging-design.md)
# ---------------------------------------------------------------------------


async def test_ask_hermes_agent_reports_the_turns_call_count_deltas() -> None:
    """tool_calls/api_calls are the bracketed GET /api/sessions delta, not the raw total."""
    counts = [(3, 9), (7, 15)]  # (tool_call_count, api_call_count): before, after

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            tool_call_count, api_call_count = counts.pop(0)
            return _session_stats_ok(tool_call_count=tool_call_count, api_call_count=api_call_count)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_REPLY_BODY
        )

    with _mock_transport(_handler):
        reply = await _ask()

    assert reply.tool_calls == 4
    assert reply.api_calls == 6


async def test_ask_hermes_agent_treats_a_missing_session_as_a_zero_baseline() -> None:
    """A fresh epoch's first turn: the "before" GET 404s, so the delta is just the after value."""
    responses = [
        _session_stats_not_found,
        lambda _r: _session_stats_ok(tool_call_count=2, api_call_count=5),
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return responses.pop(0)(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_REPLY_BODY
        )

    with _mock_transport(_handler):
        reply = await _ask()

    assert reply.tool_calls == 2
    assert reply.api_calls == 5


async def test_ask_hermes_agent_session_stats_failure_yields_none_counts() -> None:
    """A broken stats read must not fail the turn — the counts just go unknown."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_REPLY_BODY
        )

    with _mock_transport(_handler):
        reply = await _ask()

    assert reply.text == "哈囉，我是 Hermes"
    assert reply.tool_calls is None
    assert reply.api_calls is None


async def test_ask_hermes_agent_percent_encodes_the_session_id_in_the_stats_path() -> None:
    """A rotated session id (`room_key#N`) must not be truncated at a URL fragment."""
    with _serving(_REPLY_BODY) as seen:
        await ask_hermes_agent(
            "http://hermes_room_AAA:8642",
            "room_AAA#2",
            "哈囉",
            "test_key",
            idle_timeout_seconds=_IDLE,
            max_seconds=_MAX,
        )

    gets = [r for r in seen if r.method == "GET"]
    assert len(gets) == 2
    # `#` must reach the wire percent-encoded (raw_path), or httpx would treat
    # it as a URL fragment and silently drop "2" from the request entirely —
    # `.path` decodes it back to "#" for display, so raw_path is the one that
    # proves what was actually sent.
    assert all(r.url.raw_path == b"/api/sessions/room_AAA%232" for r in gets)
    assert all(r.url.fragment == "" for r in gets)


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


async def test_ask_hermes_agent_requests_a_stream_with_auth_and_session_headers() -> None:
    """The request opts into SSE and carries the Bearer token + session id."""
    with _serving(_REPLY_BODY) as seen:
        await _ask()

    request = _only_post(seen)
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

    assert json.loads(_only_post(seen).content)["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "哈囉"},
    ]


# ---------------------------------------------------------------------------
# The two budgets
# ---------------------------------------------------------------------------


async def test_ask_hermes_agent_propagates_the_idle_read_timeout() -> None:
    """Silence longer than idle_timeout_seconds surfaces as httpx.ReadTimeout."""

    def _silent(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _session_stats_not_found(request)
        raise httpx.ReadTimeout("", request=request)

    with _mock_transport(_silent), pytest.raises(httpx.ReadTimeout):
        await _ask(idle_timeout_seconds=0.05)


async def test_ask_hermes_agent_raises_timeout_error_at_the_ceiling() -> None:
    """A stream that keeps trickling forever is cut off by max_seconds."""

    async def _endless() -> AsyncIterator[bytes]:
        yield b": keepalive\n\n"
        await asyncio.sleep(30)
        yield b"data: [DONE]\n\n"

    def _trickle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _session_stats_not_found(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_endless()
        )

    with _mock_transport(_trickle), pytest.raises(TimeoutError):
        await asyncio.wait_for(_ask(max_seconds=0.05), timeout=5)


# ---------------------------------------------------------------------------
# Session deletion (the warm-up probe's cleanup)
# ---------------------------------------------------------------------------


async def _delete(session_id: str = "warmup-probe") -> None:
    """Call delete_hermes_session with the standard test arguments."""
    await delete_hermes_session("http://hermes_room_AAA:8642", session_id, "test_key")


async def test_delete_hermes_session_sends_an_authorized_delete() -> None:
    """A successful delete returns nothing and hits the session's own path."""

    def _deleted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"object": "hermes.session.deleted", "id": "warmup-probe", "deleted": True}
        )

    with _mock_transport(_deleted) as seen:
        assert await _delete() is None

    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/api/sessions/warmup-probe"
    assert seen[0].headers["Authorization"] == "Bearer test_key"


async def test_delete_hermes_session_treats_404_as_deleted() -> None:
    """Nothing to delete is the postcondition already met, not a failure."""

    def _missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    with _mock_transport(_missing):
        assert await _delete() is None


async def test_delete_hermes_session_raises_on_other_error_statuses() -> None:
    """Any non-2xx that is not a 404 surfaces to the caller."""

    def _broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    with _mock_transport(_broken), pytest.raises(httpx.HTTPStatusError):
        await _delete()


async def test_delete_hermes_session_percent_encodes_the_session_id() -> None:
    """A rotated id (`room_key#N`) must reach the wire encoded, like the GET."""

    def _deleted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"deleted": True})

    with _mock_transport(_deleted) as seen:
        await _delete("room_AAA#2")

    assert seen[0].url.raw_path == b"/api/sessions/room_AAA%232"
    assert seen[0].url.fragment == ""
