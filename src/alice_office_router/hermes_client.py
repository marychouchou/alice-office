from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = structlog.stdlib.get_logger(__name__)

# SSE framing (docs/router-hermes-agent-protocol.md 「核心請求」): every event
# is a `data: <json>` line, the stream is terminated by the sentinel below, and
# any line starting with ":" is a comment — Hermes writes `: keepalive` every 30s
# of silence, which is exactly what makes the idle timeout a liveness signal.
_DATA_PREFIX = "data:"
_DONE_SENTINEL = "[DONE]"

# finish_reason values that still carry a usable answer: "stop" is a complete
# turn, "length" a truncated one (returned, with a warning). Anything else with
# an error message is a failed turn.
_USABLE_FINISH_REASONS = frozenset({"stop", "length"})


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
    """

    model_config = ConfigDict(extra="ignore")

    text: str
    prompt_tokens: int | None = None


@dataclass
class _StreamOutcome:
    """The running result of one streamed turn, folded chunk by chunk.

    Attributes:
        parts: The `delta.content` fragments seen so far, joined by `text`.
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

    Args:
        response: The open streaming response (already checked for status).
        session_id: The turn's Hermes session id, for the skip warning.

    Returns:
        The folded outcome — text parts, chunk count, and whatever the finish
        chunk reported.
    """
    outcome = _StreamOutcome()
    async for line in response.aiter_lines():
        payload = _sse_payload(line)
        if payload is None:
            continue
        if payload == _DONE_SENTINEL:
            break
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
        (None when the server reported none). A truncated reply
        (finish_reason "length") is returned as far as it got, with a warning
        logged.

    Raises:
        httpx.HTTPError: If the request fails or the container returns a non-2xx
            status; `httpx.ReadTimeout` specifically means the stream went
            silent for longer than idle_timeout_seconds.
        TimeoutError: If the turn was still streaming after max_seconds.
        ValueError: If Hermes reported a failed turn, or the stream carried no
            reply content at all.
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
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code >= 400:
                # Nothing will iterate the body once raise_for_status fires, so
                # read it here to keep the error's response usable by the caller.
                await response.aread()
            response.raise_for_status()
            async with asyncio.timeout(max_seconds):
                outcome = await _consume_stream(response, session_id)
        status_code = response.status_code

    # The room's turn latency, the single most useful number when a user says
    # "it did not answer" (docs/logging-design.md §5.1). A failed call raises
    # instead, and is logged by core with the room context already bound.
    logger.info(
        "hermes_agent_call",
        session_id=session_id,
        status=status_code,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        chunks=outcome.chunks,
        finish_reason=outcome.finish_reason,
        prompt_tokens=outcome.prompt_tokens,
    )

    failure = outcome.failure_message()
    if failure is not None:
        raise ValueError(f"Hermes agent failed: {failure}")
    content = outcome.text
    if not content:
        raise ValueError("Hermes agent response had no content")
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
    return AgentReply(text=content, prompt_tokens=prompt_tokens)
