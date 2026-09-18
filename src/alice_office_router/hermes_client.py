from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = structlog.stdlib.get_logger(__name__)

# SSE framing (docs/router-hermes-agent-protocol.md 「核心請求」): every event
# is a `data: <json>` line, the stream is terminated by the sentinel below, and
# any line starting with ":" is a comment — Hermes writes `: keepalive` every 30s
# of silence, which is exactly what makes the idle timeout a liveness signal.
_DATA_PREFIX = "data:"
_EVENT_PREFIX = "event:"
_DONE_SENTINEL = "[DONE]"

# Hermes tags the SSE frame it writes right before running a tool with this
# named event (gateway/platforms/api_server.py's _write_real_streaming_sse) —
# the only boundary the wire format exposes between one tool-calling round and
# the next. Every other chunk, whatever round it belongs to, is an untagged
# `chat.completion.chunk` with finish_reason null, so a model's "let me try
# this" narration before a tool call is otherwise indistinguishable from its
# real final answer. See _consume_stream.
_TOOL_PROGRESS_EVENT = "hermes.tool.progress"

# finish_reason values that still carry a usable answer: "stop" is a complete
# turn, "length" a truncated one (returned, with a warning). Anything else with
# an error message is a failed turn.
_USABLE_FINISH_REASONS = frozenset({"stop", "length"})

# Budget for the /api/sessions/{id} side calls — the two GET brackets (see
# _fetch_session_counts) and the warm-up probe's DELETE (delete_hermes_session).
# They move one small JSON row and must never borrow the turn's own idle/ceiling
# budget, which can be minutes to an hour.
_SESSION_STATS_TIMEOUT = httpx.Timeout(10.0)


class _Delta(BaseModel):
    """The incremental `delta` object of one streaming choice."""

    model_config = ConfigDict(extra="ignore")

    content: str | None = None


class _StreamChoice(BaseModel):
    """One entry of a chat completion chunk's `choices` array."""

    model_config = ConfigDict(extra="ignore")

    # Defaulted rather than optional so callers never branch on "this chunk had
    # no delta" — a role-only or usage-only chunk simply contributes no content.
    delta: _Delta = Field(default_factory=_Delta)
    finish_reason: str | None = None


class _Usage(BaseModel):
    """The `usage` object of a chat completion (only prompt_tokens is read)."""

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int | None = None


class _HermesStatus(BaseModel):
    """Hermes's own outcome block, sent on the finish chunk of a streamed turn.

    Not part of the OpenAI wire format: Hermes adds it so a client can tell a
    complete turn from a truncated or failed one without guessing from
    finish_reason alone.
    """

    model_config = ConfigDict(extra="ignore")

    completed: bool | None = None
    partial: bool | None = None
    failed: bool | None = None
    error: str | None = None
    error_code: str | None = None


class _StreamError(BaseModel):
    """The optional `error` object carried by a non-"stop" finish chunk."""

    model_config = ConfigDict(extra="ignore")

    message: str | None = None
    type: str | None = None


class _ChatCompletionChunk(BaseModel):
    """Minimal view of one `chat.completion.chunk` event from Hermes.

    Every optional sub-object is defaulted to an empty instance rather than
    None, so folding a chunk into the running outcome never has to ask "was
    this field present?" — an absent block reads as all-None fields.
    """

    model_config = ConfigDict(extra="ignore")

    choices: list[_StreamChoice] = Field(default_factory=list)
    usage: _Usage = Field(default_factory=_Usage)
    hermes: _HermesStatus = Field(default_factory=_HermesStatus)
    error: _StreamError = Field(default_factory=_StreamError)


class AgentReply(BaseModel):
    """A room's Hermes agent reply plus the request's reported prompt size.

    Attributes:
        text: The assistant's reply text — always non-empty, since
            ask_hermes_agent raises rather than return a blank reply.
        prompt_tokens: The request's reported prompt_tokens, or None when the
            server didn't report a usable count. NOTE: this is the SUM across
            all of the request's internal tool-loop iterations, not the live
            context-window size (see config.SESSION_ROTATE_PROMPT_TOKENS);
            session_hygiene uses it only as an over-estimating rotation
            watermark.
        tool_calls: How many tool calls Hermes made during this turn, or None
            when it could not be determined (see _fetch_session_counts) — a
            bracket diff of the session's own cumulative counter, since the
            chat completions response carries no per-turn figure.
        api_calls: How many internal LLM API calls this turn made (the
            tool-loop iteration count), same bracket-diff caveat as
            tool_calls.
    """

    model_config = ConfigDict(extra="ignore")

    text: str
    prompt_tokens: int | None = None
    tool_calls: int | None = None
    api_calls: int | None = None


class _SessionCounts(BaseModel):
    """The two cumulative call counters `GET /api/sessions/{id}` exposes."""

    model_config = ConfigDict(extra="ignore")

    tool_call_count: int | None = None
    api_call_count: int | None = None


class _SessionEnvelope(BaseModel):
    """The `{"object": "hermes.session", "session": {...}}` response wrapper."""

    model_config = ConfigDict(extra="ignore")

    session: _SessionCounts = Field(default_factory=_SessionCounts)


class _SessionMessage(BaseModel):
    """One row of `GET /api/sessions/{id}/messages` (only the fields we read)."""

    model_config = ConfigDict(extra="ignore")

    role: str | None = None
    content: str | None = None


class _SessionMessagesEnvelope(BaseModel):
    """The `{"object": "list", "data": [...]}` response wrapper."""

    model_config = ConfigDict(extra="ignore")

    data: list[_SessionMessage] = Field(default_factory=list)


@dataclass
class _StreamOutcome:
    """The running result of one streamed turn, folded chunk by chunk.

    Attributes:
        parts: The `delta.content` fragments seen so far *in the current tool
            round*, joined by `text` — cleared each time a
            `hermes.tool.progress` event marks the start of another round, so
            a multi-round turn's final text is only what the model produced
            after its last tool call (see _consume_stream).
        chunks: How many chunk events were parsed (a liveness/volume metric
            for the call log, not part of the reply).
        finish_reason: The finish chunk's reason, None if the stream ended
            without one (a truncated connection).
        prompt_tokens: The last reported usage.prompt_tokens, raw.
        hermes: The finish chunk's Hermes status block (all-None when absent).
        error: The finish chunk's error object (all-None when absent).
    """

    parts: list[str] = field(default_factory=list)
    chunks: int = 0
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    hermes: _HermesStatus = field(default_factory=_HermesStatus)
    error: _StreamError = field(default_factory=_StreamError)

    @property
    def text(self) -> str:
        """Return the assembled reply text.

        Returns:
            Every content fragment concatenated in arrival order; "" when the
            stream carried no content at all.
        """
        return "".join(self.parts)

    def apply(self, chunk: _ChatCompletionChunk) -> None:
        """Fold one parsed chunk into this outcome.

        Args:
            chunk: The parsed `chat.completion.chunk` event.
        """
        self.chunks += 1
        choice = chunk.choices[0] if chunk.choices else _StreamChoice()
        if choice.delta.content:
            self.parts.append(choice.delta.content)
        if chunk.usage.prompt_tokens is not None:
            self.prompt_tokens = chunk.usage.prompt_tokens
        if choice.finish_reason is None:
            return
        # The finish chunk is the only one that states how the turn ended.
        self.finish_reason = choice.finish_reason
        self.hermes = chunk.hermes
        self.error = chunk.error

    def failure_message(self) -> str | None:
        """Return why the turn failed, or None when it produced a usable answer.

        Returns:
            The most specific error text available (Hermes's own error, else
            the OpenAI-style error message, else the finish reason itself), or
            None when the turn finished with a usable answer.
        """
        reported = self.hermes.error or self.error.message
        if self.hermes.failed:
            return reported or self.hermes.error_code or "unknown error"
        if self.finish_reason not in _USABLE_FINISH_REASONS and reported:
            return reported
        return None


async def _fetch_session_counts(
    client: httpx.AsyncClient, base_url: str, session_id: str, api_key: str
) -> tuple[int | None, int | None]:
    """Read a session's cumulative tool-call and API-call counters from Hermes.

    `POST /v1/chat/completions` reports neither figure (its usage block is the
    OpenAI-shaped token triad only — docs/router-hermes-agent-protocol.md).
    `GET /api/sessions/{id}` is the only place Hermes exposes them, and only as
    running totals for the whole session, so `ask_hermes_agent` calls this once
    before and once after its own request and diffs the two readings to get
    this turn's contribution (`_count_delta`).

    Args:
        client: The turn's own httpx client (reused for connection pooling;
            this call overrides its timeout, see _SESSION_STATS_TIMEOUT).
        base_url: Base URL of the Hermes agent container.
        session_id: The session whose counters to read.
        api_key: Bearer token matching the container's API_SERVER_KEY.

    Returns:
        (tool_call_count, api_call_count). Both 0 when the session does not
        exist yet — Hermes creates it lazily on the first chat completions
        call, not before, so a fresh epoch's first turn always starts from a
        real zero baseline rather than an unknown one. Both None when the read
        itself failed (network error, unexpected response shape); callers must
        treat that as "unknown", never as zero.
    """
    url = f"{base_url}/api/sessions/{quote(session_id, safe='')}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = await client.get(url, headers=headers, timeout=_SESSION_STATS_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning(
            "hermes_session_stats_unavailable", session_id=session_id, error=type(exc).__name__
        )
        return None, None
    if response.status_code == 404:
        return 0, 0
    if response.status_code >= 400:
        logger.warning(
            "hermes_session_stats_unavailable",
            session_id=session_id,
            status=response.status_code,
        )
        return None, None
    try:
        parsed = _SessionEnvelope.model_validate_json(response.content)
    except ValidationError as exc:
        logger.warning(
            "hermes_session_stats_unavailable", session_id=session_id, errors=exc.error_count()
        )
        return None, None
    return parsed.session.tool_call_count, parsed.session.api_call_count


async def _fetch_last_assistant_text(
    client: httpx.AsyncClient, base_url: str, session_id: str, api_key: str
) -> str | None:
    """Read the session's own message history and return its last assistant reply.

    Fallback for a stream that ends with a usable finish_reason but carried no
    content at all — observed when a turn ends via Hermes's own max-iterations
    summary (`agent.chat_completion_helpers.handle_max_iterations`), which asks
    the model for one last answer through a *non-streaming*
    `chat.completions.create()` call that writes straight to Hermes's own
    state.db without ever reaching the SSE stream this client reads. The
    answer is real and already in Hermes's own history — `GET
    /api/sessions/{id}/messages` returns the whole session, unpaginated — so
    this is read from there instead of treating the turn as a hard failure.

    Args:
        client: The turn's own httpx client (reused for connection pooling).
        base_url: Base URL of the Hermes agent container.
        session_id: The session whose history to read.
        api_key: Bearer token matching the container's API_SERVER_KEY.

    Returns:
        The last message with `role: "assistant"` and non-empty content
        (an assistant message mid-tool-call has empty content, so those are
        skipped), or None when the read failed or no such message exists.
    """
    url = f"{base_url}/api/sessions/{quote(session_id, safe='')}/messages"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = await client.get(url, headers=headers, timeout=_SESSION_STATS_TIMEOUT)
        response.raise_for_status()
        parsed = _SessionMessagesEnvelope.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError) as exc:
        logger.warning(
            "hermes_session_messages_unavailable", session_id=session_id, error=type(exc).__name__
        )
        return None
    for message in reversed(parsed.data):
        if message.role == "assistant" and message.content:
            return message.content
    return None


async def delete_hermes_session(base_url: str, session_id: str, api_key: str) -> None:
    """Delete one Hermes session, dropping its row and messages from state.db.

    The only call in this router that deletes anything on the Hermes side, and it
    exists for exactly one reason: the warm-up probe (see core._probe_agent and
    docs/router-hermes-agent-protocol.md 「暖機探針」). A freshly started Hermes
    process pays a one-time tool-registry probe on its first chat turn, so the
    gate's warm-up spends one throwaway turn on its own session id to take that
    cost off the user's real first message. That turn must not survive: Hermes's
    `session_search` tool reads across every session in the room, so an
    undeleted probe would surface as room history.

    Args:
        base_url: Base URL of the Hermes agent container.
        session_id: The session to delete. Percent-encoded into the path like
            the GET, so a `#` (the rotated `room_key#N` form) is not cut off as
            a URL fragment.
        api_key: Bearer token matching the container's API_SERVER_KEY.

    Raises:
        httpx.HTTPStatusError: If Hermes answered with a non-2xx status other
            than 404.
        httpx.HTTPError: If the request itself failed (connection, timeout).
    """
    url = f"{base_url}/api/sessions/{quote(session_id, safe='')}"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=_SESSION_STATS_TIMEOUT) as client:
        response = await client.delete(url, headers=headers)
    # 404 is success: there was nothing to delete. Hermes creates a session
    # lazily on its first chat completion, so a probe that failed before that
    # left nothing behind — the caller's postcondition ("no probe session in
    # this room") already holds.
    if response.status_code != 404:
        response.raise_for_status()
    logger.info("hermes_session_deleted", session_id=session_id, status=response.status_code)


def _count_delta(after: int | None, before: int | None) -> int | None:
    """Return how much a cumulative session counter grew during one call.

    Args:
        after: The counter read once the call finished, or None if that read
            failed.
        before: The counter read just before the call started, or None if
            that read failed.

    Returns:
        `after - before` when both reads succeeded and the counter did not go
        backwards (a session's counters only ever grow); None otherwise, so a
        failed reading never gets silently reported as "zero calls".
    """
    if after is None or before is None or after < before:
        return None
    return after - before


def _build_messages(text: str, system: str | None) -> list[dict[str, str]]:
    """Build the chat `messages` array, prepending a system message if given.

    Args:
        text: The user message content.
        system: An ephemeral system message to layer on top of the room's core
            prompt for this one turn, or None for the plain user-only request.

    Returns:
        `[user]` when `system` is None, else `[system, user]`.
    """
    user_message = {"role": "user", "content": text}
    if system is None:
        return [user_message]
    return [{"role": "system", "content": system}, user_message]


def _sse_payload(line: str) -> str | None:
    """Extract the JSON payload of one SSE line.

    Args:
        line: One line off the event stream, without its line terminator.

    Returns:
        The text after `data:`, or None for anything that carries no payload —
        the blank lines that separate events, the `: keepalive` comments Hermes
        writes while the agent is still working, and any other SSE field.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(":") or not stripped.startswith(_DATA_PREFIX):
        return None
    return stripped[len(_DATA_PREFIX) :].strip() or None


async def _consume_stream(response: httpx.Response, session_id: str) -> _StreamOutcome:
    """Read the whole event stream and fold it into a single outcome.

    A data line that will not parse is skipped rather than fatal: one corrupt
    frame must not cost the user a turn whose remaining chunks are fine.

    Hermes flattens an entire multi-round tool-calling turn onto one stream
    with no per-round finish_reason, so a round boundary is only visible as a
    named `hermes.tool.progress` event (see _TOOL_PROGRESS_EVENT). Each time
    one arrives, the content collected so far is discarded: it was narration
    the model produced before deciding to call a tool, not its answer. Only
    what streams in after the *last* such event survives to become the reply
    the user sees — the rest still reaches Hermes's own state.db, this just
    keeps it out of the message we relay.

    Args:
        response: The open streaming response (already checked for status).
        session_id: The turn's Hermes session id, for the skip warning.

    Returns:
        The folded outcome — text parts (last round only), chunk count, and
        whatever the finish chunk reported.
    """
    outcome = _StreamOutcome()
    event_name: str | None = None
    async for line in response.aiter_lines():
        stripped = line.strip()
        if not stripped:
            event_name = None  # blank line ends the current SSE frame
            continue
        if stripped.startswith(_EVENT_PREFIX):
            event_name = stripped[len(_EVENT_PREFIX) :].strip()
            continue
        payload = _sse_payload(stripped)
        if payload is None:
            continue
        if payload == _DONE_SENTINEL:
            break
        if event_name == _TOOL_PROGRESS_EVENT:
            outcome.parts.clear()
            continue
        try:
            chunk = _ChatCompletionChunk.model_validate_json(payload)
        except ValidationError as exc:
            # Never log the payload itself: a data line is reply content, which
            # must not reach the log stream (docs/logging-design.md §5.7).
            logger.warning(
                "hermes_agent_chunk_skipped", session_id=session_id, errors=exc.error_count()
            )
            continue
        outcome.apply(chunk)
    return outcome


def _resolve_reply_text(
    outcome: _StreamOutcome, recovered_content: str | None, session_id: str
) -> str:
    """Decide a turn's final reply text, or raise why it has none.

    Args:
        outcome: The folded stream outcome.
        recovered_content: The session-history fallback's answer (see
            _fetch_last_assistant_text), or None when the stream itself
            carried text, or when it didn't and the fallback found nothing
            either.
        session_id: The turn's session id, for the recovery warning log.

    Returns:
        `outcome.text` when non-empty; otherwise `recovered_content`, logged
        as a warning since this path means the SSE stream itself came back
        empty (observed on Hermes's own max-iterations summary path, which
        answers outside the stream — see hermes-max-iterations-no-content-bug).

    Raises:
        ValueError: If Hermes reported a failed turn, or neither the stream
            nor the history fallback carried any content.
    """
    failure = outcome.failure_message()
    if failure is not None:
        raise ValueError(f"Hermes agent failed: {failure}")
    if outcome.text:
        return outcome.text
    if recovered_content:
        logger.warning(
            "hermes_agent_stream_empty_recovered_from_history",
            session_id=session_id,
            finish_reason=outcome.finish_reason,
            chars=len(recovered_content),
        )
        return recovered_content
    raise ValueError("Hermes agent response had no content")


async def ask_hermes_agent(
    base_url: str,
    session_id: str,
    text: str,
    api_key: str,
    *,
    idle_timeout_seconds: float,
    max_seconds: float,
    system: str | None = None,
) -> AgentReply:
    """Send a user message to a room's Hermes agent and return its reply.

    Uses the agent's built-in OpenAI-compatible api_server platform in
    streaming mode (`"stream": true`). Streaming is not used to show partial
    text — the reply is still delivered in one piece — but to get a liveness
    signal: Hermes emits a `: keepalive` comment after every 30s of silence,
    including while a tool is running, so "the agent is alive" becomes "bytes
    keep arriving" and the router can wait for a legitimately long turn without
    a total-duration cap that would orphan good answers.

    The session_id is sent as the Hermes session id, so a room's container keeps
    conversation continuity across messages; the router derives it per turn from
    the room key and the room's current session epoch (see session_hygiene).

    Args:
        base_url: Base URL of the Hermes agent container (e.g. http://hermes_room_AAA:8642).
        session_id: The X-Hermes-Session-Id to route this turn to (the room key,
            or room_key#epoch after a rotation).
        text: User message text to send.
        api_key: Bearer token matching the container's API_SERVER_KEY.
        idle_timeout_seconds: Maximum silence between bytes before the agent is
            considered dead (the HTTP read timeout) — the router passes
            config.HERMES_IDLE_TIMEOUT_SECONDS. Required with no default so
            every caller states its own budget.
        max_seconds: Absolute ceiling on the whole turn, a safety valve that a
            live agent should never reach — the router passes
            config.HERMES_REQUEST_TIMEOUT_SECONDS. Connecting to the container
            stays on a short fixed budget.
        system: Optional ephemeral system message prepended for this one turn
            (used by the group path to carry GROUP_SYSTEM_PROMPT); None sends
            the plain user-only request, byte-identical to the 1:1 path.

    Returns:
        The assistant's reply text plus the request's reported prompt_tokens
        (None when the server reported none), and this turn's tool_calls /
        api_calls counts bracketed from GET /api/sessions/{id} (None when
        that read failed — see _fetch_session_counts). A truncated reply
        (finish_reason "length") is returned as far as it got, with a warning
        logged.

    Raises:
        httpx.HTTPError: If the request fails or the container returns a non-2xx
            status; `httpx.ReadTimeout` specifically means the stream went
            silent for longer than idle_timeout_seconds.
        TimeoutError: If the turn was still streaming after max_seconds.
        ValueError: If Hermes reported a failed turn, or the stream carried no
            reply content and the session-history fallback
            (_fetch_last_assistant_text) found none either.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Hermes-Session-Id": session_id,
    }
    payload = {"messages": _build_messages(text, system), "stream": True}
    url = f"{base_url}/v1/chat/completions"

    started = time.perf_counter()
    timeout = httpx.Timeout(connect=10.0, read=idle_timeout_seconds, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        before_tool_calls, before_api_calls = await _fetch_session_counts(
            client, base_url, session_id, api_key
        )
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code >= 400:
                # Nothing will iterate the body once raise_for_status fires, so
                # read it here to keep the error's response usable by the caller.
                await response.aread()
            response.raise_for_status()
            async with asyncio.timeout(max_seconds):
                outcome = await _consume_stream(response, session_id)
        status_code = response.status_code
        after_tool_calls, after_api_calls = await _fetch_session_counts(
            client, base_url, session_id, api_key
        )
        # Still inside the client's context, so this reuses the same pooled
        # connection: a stream that carried no text but no failure either
        # (the max-iterations summary path — see _fetch_last_assistant_text)
        # has its real answer sitting in Hermes's own session history.
        recovered_content: str | None = None
        if not outcome.text and outcome.failure_message() is None:
            recovered_content = await _fetch_last_assistant_text(
                client, base_url, session_id, api_key
            )

    tool_calls = _count_delta(after_tool_calls, before_tool_calls)
    api_calls = _count_delta(after_api_calls, before_api_calls)

    # The room's turn latency, the single most useful number when a user says
    # "it did not answer" (docs/logging-design.md §5.1). tool_calls/api_calls
    # turn a "why did this take 15 minutes" question into a number an operator
    # can read straight off this line instead of SSHing into the container to
    # read logs/agent.log (docs/router-hermes-agent-protocol.md). A failed call
    # raises instead, and is logged by core with the room context already bound.
    logger.info(
        "hermes_agent_call",
        session_id=session_id,
        status=status_code,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        chunks=outcome.chunks,
        finish_reason=outcome.finish_reason,
        prompt_tokens=outcome.prompt_tokens,
        tool_calls=tool_calls,
        api_calls=api_calls,
    )

    content = _resolve_reply_text(outcome, recovered_content, session_id)
    if outcome.finish_reason == "length" or outcome.hermes.partial:
        # Delivered anyway: half an answer beats none, and the user can ask for
        # the rest. Recurring truncation is an LLM max-tokens problem, not a
        # router one — hence a warning an operator can count, not an exception.
        logger.warning(
            "hermes_agent_truncated",
            session_id=session_id,
            finish_reason=outcome.finish_reason,
            chars=len(content),
        )

    # A reported 0 is the OpenAI-compatible server's "didn't count" default, not
    # a real zero-token prompt; normalize any non-positive value to None here so
    # the rotation watermark never reads it as a genuine measurement.
    reported = outcome.prompt_tokens
    prompt_tokens = reported if reported is not None and reported > 0 else None
    return AgentReply(
        text=content, prompt_tokens=prompt_tokens, tool_calls=tool_calls, api_calls=api_calls
    )
