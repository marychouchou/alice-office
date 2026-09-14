"""Channel-free orchestration: gate -> container -> agent -> reply texts.

`process_inbound` is the single entry point every channel adapter funnels into
once it has parsed its own wire format into an `InboundMessage`. It runs the
Google OAuth gate, resolves the room's Hermes agent container, asks the agent,
and *returns* the plain-text messages to send back to the room — it never
touches any channel's send API or reply tokens, so it stays directly unit
testable and reusable across adapters (see docs/channel-interface-design.md).

Alongside the texts it returns a `TurnEnvelope` draft describing how the turn
went (docs/logging-design.md §5.7). Core builds it but does not emit it: only
the adapter knows whether the reply actually reached the room, so the adapter
fills `delivered` and calls `conversation_log.record_turn`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace

import httpx
import structlog
from pydantic import ValidationError
from structlog.contextvars import bound_contextvars

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings
from alice_office_router.container_manager import get_or_create_container
from alice_office_router.conversation_log import Outcome, TurnEnvelope
from alice_office_router.google_oauth import check_google_authorization
from alice_office_router.group_context import (
    GROUP_SYSTEM_PROMPT,
    build_group_prompt,
    clear_observed,
    is_silence,
    peek_observed,
    record_observed,
)
from alice_office_router.hermes_client import ask_hermes_agent
from alice_office_router.session_hygiene import (
    HANDOFF_PROMPT,
    RESET_CONFIRMATION,
    begin_turn,
    build_turn_text,
    check_reset_command,
    complete_turn,
    reset_session,
    session_id_for,
)

logger = logging.getLogger(__name__)

# Cap on the free-text half of an error string. `error` rides the log stream off
# the host into Loki, where message content must never go (conversation_log's
# module doc), so what an exception's str() happens to contain is not safe to
# forward whole — a few hundred characters of a stack-free message is all an
# operator reads anyway.
_ERROR_DETAIL_MAX_CHARS = 200

# Outcomes whose inbound text Hermes already wrote to the room's state.db, so
# the envelope must not keep a second copy: "replied", and "silence" — the agent
# answered, it just answered with the silence token. "agent_failed" is
# deliberately NOT here: half its cases (container down, connection refused)
# never reached Hermes, and then this envelope is the only record of what the
# user said (see scripts/conversations.py `_is_missing_from_state_db`).
_TEXT_IN_STATE_DB: frozenset[Outcome] = frozenset({"replied", "silence"})


def _describe_error(origin: str, exc: Exception) -> str:
    """Render an exception for the `error` field without leaking message content.

    `str(exc)` is not safe to forward. A pydantic `ValidationError` embeds the
    value it rejected — for this router that is the agent's reply text, i.e. the
    exact thing the log stream must not carry — and some httpx timeouts stringify
    to nothing at all, which would leave `error` empty and useless. The type name
    is always present and always safe; the message is truncated; a validation
    error contributes only its shape.

    Args:
        origin: Which stage failed ("container", "agent").
        exc: The exception that ended the turn.

    Returns:
        A short, content-free reason string, e.g. "agent: ReadTimeout" or
        "agent: ValidationError (1 error at text)".
    """
    name = type(exc).__name__
    if isinstance(exc, ValidationError):
        fields = ", ".join(
            ".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors()
        )
        return f"{origin}: {name} ({exc.error_count()} error(s) at {fields})"
    detail = str(exc)[:_ERROR_DETAIL_MAX_CHARS].strip()
    return f"{origin}: {name}: {detail}" if detail else f"{origin}: {name}"


@dataclass(frozen=True)
class AgentTurn:
    """The result of one agent-bound turn, reply text plus what to record.

    Attributes:
        outcome: How the turn ended (conversation_log.Outcome). An agent-bound
            turn only ever produces three of them: "replied" when the agent
            answered and the answer is deliverable, "agent_failed" when the
            container or the agent call failed, "silence" when a group reply was
            the silence token.
        text: The deliverable reply, or None for the other two outcomes.
        session_id: The exact X-Hermes-Session-Id sent (the join key onto
            state.db), or None when the call never got that far.
        rotated: Whether this turn rotated the room to a fresh session epoch.
        duration_ms: Wall time of the agent HTTP call, None if it never ran.
        prompt_tokens: The reply's reported prompt_tokens, if any.
        error: Short reason string when the turn failed; None otherwise.
    """

    outcome: Outcome
    text: str | None = None
    session_id: str | None = None
    rotated: bool = False
    duration_ms: float | None = None
    prompt_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class RouteResult:
    """What `_route` decided for one inbound message, before delivery.

    Attributes:
        texts: Texts to send back to the room, in delivery order.
        outcome: How the turn ended (see conversation_log.Outcome).
        gate_status: The Google OAuth gate's verdict, or None when the gate
            was short-circuited (observe, reset).
        session_id: The session id sent to Hermes, if an agent call was made.
        rotated: Whether this turn rotated the room's session epoch.
        agent_duration_ms: Wall time of the agent HTTP call, if it ran.
        prompt_tokens: The reply's reported prompt_tokens, if any.
        error: Short reason string when the turn failed; None otherwise.
    """

    texts: list[str] = field(default_factory=list)
    outcome: Outcome = "replied"
    gate_status: str | None = None
    session_id: str | None = None
    rotated: bool = False
    agent_duration_ms: float | None = None
    prompt_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class InboundResult:
    """What `process_inbound` hands back to the calling adapter.

    Attributes:
        texts: Texts to send back to the room, in delivery order.
        envelope: The turn's record, complete except for `delivered` — the
            adapter fills that in after sending and calls record_turn.
    """

    texts: list[str]
    envelope: TurnEnvelope


def _elapsed_ms(started: float) -> float:
    """Return milliseconds elapsed since a perf_counter reading.

    Args:
        started: The `time.perf_counter()` value taken before the call.

    Returns:
        Elapsed wall time in milliseconds, rounded to 2 decimals.
    """
    return round((time.perf_counter() - started) * 1000, 2)


def _context_value(key: str) -> str | None:
    """Read one string field out of the bound structlog context.

    Args:
        key: The contextvar name ("request_id", "event_id").

    Returns:
        The bound value when it is a string, else None — so a turn running
        outside any HTTP request simply records None instead of branching.
    """
    value = structlog.contextvars.get_contextvars().get(key)
    return value if isinstance(value, str) else None


async def _generate_handoff(
    target_url: str, room_key: str, retired_epoch: int, config: Settings
) -> str | None:
    """Best-effort: ask the just-retired session for a one-shot handoff summary.

    Sends one extra request to the retired epoch's session id (the rotation has
    already happened in begin_turn) asking for a short summary of unfinished
    items, preferences, and in-progress tasks. Any failure is logged and
    swallowed — the new epoch then continues clean-slate, since a fresh session
    with no summary still beats an ever-growing one. The summary is never
    persisted: it exists only to be injected into this turn's user message.

    Args:
        target_url: The room's Hermes container base URL.
        room_key: The room key core routes on.
        retired_epoch: The epoch just closed (whose session is summarized).
        config: Application settings.

    Returns:
        The handoff summary text, or None when the summary request failed.
    """
    old_session_id = session_id_for(room_key, retired_epoch)
    try:
        reply = await ask_hermes_agent(
            target_url, old_session_id, HANDOFF_PROMPT, config.HERMES_API_SERVER_KEY
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            f"Handoff summary failed for room {room_key}; continuing clean-slate ({exc})"
        )
        return None
    return reply.text


async def _ask_agent(
    room_key: str, text: str, config: Settings, *, system: str | None = None
) -> AgentTurn:
    """Resolve the room's Hermes container, rotate if due, and ask for a reply.

    Each step is independently guarded: a failure is logged and yields an
    "agent_failed" AgentTurn (the caller then delivers nothing for it),
    mirroring the original background-task contract where a downstream error
    must never propagate. Session hygiene is applied here so 1:1 and group turns
    share it: begin_turn evaluates the triggers and rotates atomically (before
    any await); a rotated turn then fetches a one-shot handoff summary from the
    retired epoch's session and folds it into this turn's user text; a
    successful turn records its token watermark (see session_hygiene, including
    the accepted trade-offs of the non-persisted handoff).

    Args:
        room_key: Unique room key used to resolve the container and session.
        text: User message text to forward to the agent.
        config: Application settings.
        system: Optional ephemeral system message for this turn (the group
            path passes GROUP_SYSTEM_PROMPT); None keeps the 1:1 request plain.

    Returns:
        An AgentTurn carrying the reply (or the failure) plus the session id,
        rotation flag, latency and token count the envelope records.
    """
    try:
        target_url = get_or_create_container(room_key, config)
    except Exception as exc:
        reason = _describe_error("container", exc)
        logger.error(f"Failed to get/create container for room {room_key}: {reason}")
        return AgentTurn(outcome="agent_failed", error=reason)

    plan = begin_turn(config, room_key)
    # retired_epoch is set exactly when this turn rotated (see TurnPlan).
    handoff = (
        await _generate_handoff(target_url, room_key, plan.retired_epoch, config)
        if plan.retired_epoch is not None
        else None
    )

    session_id = session_id_for(room_key, plan.epoch)
    started = time.perf_counter()
    try:
        result = await ask_hermes_agent(
            target_url,
            session_id,
            build_turn_text(handoff, text),
            config.HERMES_API_SERVER_KEY,
            system=system,
        )
    except (httpx.HTTPError, ValueError) as exc:
        reason = _describe_error("agent", exc)
        logger.error(f"Hermes agent request failed for room {room_key}: {reason}")
        return AgentTurn(
            outcome="agent_failed",
            session_id=session_id,
            rotated=plan.rotated,
            duration_ms=_elapsed_ms(started),
            error=reason,
        )

    complete_turn(config, room_key, epoch=plan.epoch, prompt_tokens=result.prompt_tokens)
    return AgentTurn(
        outcome="replied",
        text=result.text,
        session_id=session_id,
        rotated=plan.rotated,
        duration_ms=_elapsed_ms(started),
        prompt_tokens=result.prompt_tokens,
    )


async def _ask_group_agent(msg: InboundMessage, config: Settings) -> AgentTurn:
    """Ask the agent for an addressed group message, managing buffer and silence.

    Folds the room's observed background into a tagged prompt (design §7), asks
    the agent under the group system message, then clears only the records that
    were folded in (a failure keeps the whole context for a retry; and any
    unaddressed message observed during the agent call survives, since only the
    peeked records are dropped — see group_context.clear_observed), and drops a
    silence-token reply.

    Args:
        msg: The addressed group inbound message.
        config: Application settings.

    Returns:
        The AgentTurn from the underlying call, re-labelled "silence" (with no
        text) when the agent deliberately chose not to answer.
    """
    observed = peek_observed(config, msg.room_key)
    prompt = build_group_prompt(observed, msg)
    turn = await _ask_agent(msg.room_key, prompt, config, system=GROUP_SYSTEM_PROMPT)
    if turn.text is None:
        return turn
    clear_observed(config, msg.room_key, observed)
    if is_silence(turn.text):
        return replace(turn, outcome="silence", text=None)
    return turn


async def _reply_for(msg: InboundMessage, config: Settings) -> AgentTurn:
    """Ask the agent for a reply, taking the group path for group messages.

    Args:
        msg: The inbound message (already past the observe short-circuit, so a
            group message here is one addressed to the bot).
        config: Application settings.

    Returns:
        The turn's AgentTurn; its `text` is None when nothing should be
        delivered.
    """
    if msg.is_group:
        return await _ask_group_agent(msg, config)
    return await _ask_agent(msg.room_key, msg.text, config)


async def _route(msg: InboundMessage, config: Settings) -> RouteResult:
    """Decide what one inbound message produces, without delivering anything.

    The whole pipeline in outcome order: an unaddressed group message is only
    observed; a manual reset command rotates the room's session and confirms
    without an agent turn; the Google gate can block; otherwise the agent runs
    (its "notice" message, if any, riding ahead of the reply).

    Args:
        msg: The normalized inbound message (identity + plain text).
        config: Application settings.

    Returns:
        A RouteResult with the texts to deliver and everything the turn
        envelope records about how the turn went.
    """
    # Observe short-circuit, before the OAuth gate: an unaddressed group
    # message must neither ask the agent nor trigger an auth prompt; a
    # blocked room still accumulates background to carry once authorized.
    if msg.is_group and not msg.addressed:
        record_observed(config, msg.room_key, msg.sender_id, msg.sender_name, msg.text)
        return RouteResult(outcome="observed")

    # Manual session reset, before the OAuth gate: rotate to a fresh epoch
    # (no handoff — a deliberate clean slate), drop any group background so
    # it can't leak into the new epoch, and confirm without an agent turn.
    if check_reset_command(msg, config):
        reset_session(config, msg.room_key)
        clear_observed(config, msg.room_key, peek_observed(config, msg.room_key))
        return RouteResult(texts=[RESET_CONFIRMATION], outcome="reset")

    status, message = check_google_authorization(msg.room_key, config)
    if status == "blocked" and message is not None:
        return RouteResult(texts=[message], outcome="blocked", gate_status=status)

    texts: list[str] = []
    if status == "notice" and message is not None:
        texts.append(message)

    turn = await _reply_for(msg, config)
    if turn.text is not None:
        texts.append(turn.text)
    return RouteResult(
        texts=texts,
        outcome=turn.outcome,
        gate_status=status,
        session_id=turn.session_id,
        rotated=turn.rotated,
        agent_duration_ms=turn.duration_ms,
        prompt_tokens=turn.prompt_tokens,
        error=turn.error,
    )


def _draft_envelope(msg: InboundMessage, result: RouteResult) -> TurnEnvelope:
    """Build the turn's envelope draft, everything but `delivered`.

    Args:
        msg: The inbound message this turn handled.
        result: What `_route` decided for it.

    Returns:
        A TurnEnvelope with `delivered=None` for the adapter to fill in.
        `inbound_text` is carried only for the outcomes Hermes did not record
        itself (`_TEXT_IN_STATE_DB`) — duplicating a turn that state.db already
        holds in full would just be a second copy of the content (§5.7).
    """
    return TurnEnvelope(
        request_id=_context_value("request_id"),
        event_id=_context_value("event_id"),
        channel=msg.channel,
        room_key=msg.room_key,
        session_id=result.session_id,
        outcome=result.outcome,
        inbound_text=None if result.outcome in _TEXT_IN_STATE_DB else msg.text,
        is_group=msg.is_group,
        addressed=msg.addressed,
        sender_id=msg.sender_id,
        sender_name=msg.sender_name,
        gate_status=result.gate_status,
        rotated=result.rotated,
        agent_duration_ms=result.agent_duration_ms,
        prompt_tokens=result.prompt_tokens,
        error=result.error,
    )


async def process_inbound(msg: InboundMessage, config: Settings) -> InboundResult:
    """Run the channel-free pipeline for one inbound message.

    A thin wrapper over `_route` that binds the room's log context and turns
    the routing decision into the adapter's two deliverables: the texts to
    send, and the turn envelope to record after sending.

    Args:
        msg: The normalized inbound message (identity + plain text).
        config: Application settings.

    Returns:
        An InboundResult. `texts` are in delivery order: an unaddressed group
        message is only observed and returns nothing; a manual reset command
        rotates the room's session and returns only the fixed confirmation
        (agent not called); gate "blocked" returns only the authorization
        message (agent not called); "notice" returns the notice followed by the
        agent reply; "ok" returns just the agent reply. A container/agent
        failure (or a silence-token group reply) drops the agent reply, keeping
        any notice. `envelope` records which of those happened.
    """
    # Every line logged downstream of here — gate, container, agent, session —
    # carries this room_key (docs/logging-design.md §5.1); unbound on exit, so a
    # background task handling another room never inherits it.
    with bound_contextvars(room_key=msg.room_key):
        result = await _route(msg, config)
        return InboundResult(texts=result.texts, envelope=_draft_envelope(msg, result))
