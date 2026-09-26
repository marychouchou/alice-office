"""One agent-bound turn: how it runs against the room's Hermes agent, and how it fails.

`ask_agent` is everything between "this message should reach the agent" and
"here is what to tell the room": resolve the room's container, apply session
hygiene (rotate the epoch when due, fetch the one-shot handoff summary from the
retired session), make the Hermes call, and turn every way that can fail into a
fixed, channel-free notice for the room (`AGENT_TIMEOUT_NOTICE`,
`AGENT_FAILURE_NOTICE`) plus a content-free `error` for the log. It returns an
`AgentTurn` and never raises for a downstream failure.

Split out of core so that changing how a turn runs or fails does not mean
reading the dispatch pipeline: core decides *whether* and *under which system
prompt* the agent is asked (and holds the room's turn lock around it); this
module owns the call itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from alice_office_router.config import Settings
from alice_office_router.container_manager import get_or_create_container
from alice_office_router.conversation_log import Outcome, describe_error
from alice_office_router.hermes_client import ask_hermes_agent
from alice_office_router.session_hygiene import (
    HANDOFF_PROMPT,
    begin_turn,
    build_turn_text,
    complete_turn,
    session_id_for,
)

logger = logging.getLogger(__name__)

# What the room is told when the router gave up waiting for the turn (the
# stream went silent for HERMES_IDLE_TIMEOUT_SECONDS, or the whole turn passed
# the HERMES_REQUEST_TIMEOUT_SECONDS ceiling). The agent itself is not
# interrupted, so its answer still lands in the room's Hermes session and a
# repeat question is cheap — the wording says so. Channel-free: no LINE-specific
# wording, every adapter sends it as plain text.
AGENT_TIMEOUT_NOTICE = (
    "這題處理時間超過限制，這次的回覆沒有送出。請再問我一次，可以把問題縮小或分段，我會接著處理。"
)

# What the room is told for every other agent-bound failure (container could not
# be created or reached, HTTP error, unusable response body). Deliberately
# generic: the actionable detail belongs in the log's `error` field, not in the
# room. Channel-free, like AGENT_TIMEOUT_NOTICE.
AGENT_FAILURE_NOTICE = "系統暫時無法回應，請稍後再試一次。"


@dataclass(frozen=True)
class AgentTurn:
    """The result of one agent-bound turn, reply text plus what to record.

    Attributes:
        outcome: How the turn ended (conversation_log.Outcome). An agent-bound
            turn only ever produces three of them: "replied" when the agent
            answered and the answer is deliverable, "agent_failed" when the
            container or the agent call failed, "silence" when a group reply was
            the silence token.
        text: The text to deliver to the room — the agent's reply for
            "replied", and the fixed notice (AGENT_TIMEOUT_NOTICE or
            AGENT_FAILURE_NOTICE) for "agent_failed", so a failed turn is
            never answered with silence. None only for "silence", where the
            agent deliberately chose not to answer.
        session_id: The exact X-Hermes-Session-Id sent (the join key onto
            state.db), or None when the call never got that far.
        rotated: Whether this turn rotated the room to a fresh session epoch.
        duration_ms: Wall time of the agent HTTP call, None if it never ran.
        prompt_tokens: The reply's reported prompt_tokens, if any.
        tool_calls: How many tool calls Hermes made answering this turn, or
            None when the call never ran or the count could not be read (see
            hermes_client.AgentReply).
        api_calls: How many internal LLM API calls this turn made, same
            caveats as tool_calls.
        error: Short reason string when the turn failed; None otherwise.
    """

    outcome: Outcome
    text: str | None = None
    session_id: str | None = None
    rotated: bool = False
    duration_ms: float | None = None
    prompt_tokens: int | None = None
    tool_calls: int | None = None
    api_calls: int | None = None
    error: str | None = None


def elapsed_ms(started: float) -> float:
    """Return milliseconds elapsed since a perf_counter reading.

    Args:
        started: The `time.perf_counter()` value taken before the call.

    Returns:
        Elapsed wall time in milliseconds, rounded to 2 decimals.
    """
    return round((time.perf_counter() - started) * 1000, 2)


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
            target_url,
            old_session_id,
            HANDOFF_PROMPT,
            config.HERMES_API_SERVER_KEY,
            idle_timeout_seconds=config.HERMES_IDLE_TIMEOUT_SECONDS,
            max_seconds=config.HERMES_REQUEST_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        logger.warning(
            f"Handoff summary failed for room {room_key}; continuing clean-slate ({exc})"
        )
        return None
    return reply.text


async def ask_agent(
    room_key: str, text: str, config: Settings, *, system: str | None = None
) -> AgentTurn:
    """Resolve the room's Hermes container, rotate if due, and ask for a reply.

    Each step is independently guarded: a failure is logged and yields an
    "agent_failed" AgentTurn carrying a fixed notice for the room (a timeout
    gets its own wording), mirroring the original background-task contract
    where a downstream error must never propagate — but never answering the
    user with silence. Session hygiene is applied here so 1:1 and group turns
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
            path passes GROUP_SYSTEM_PROMPT, the 1:1 path
            DIRECT_SYSTEM_PROMPT); None sends the room's own prompt alone.

    Returns:
        An AgentTurn carrying the reply (or the failure) plus the session id,
        rotation flag, latency and token count the envelope records.
    """
    try:
        # Off-loop: the call blocks up to _READY_TIMEOUT_SECONDS on a cold
        # room, and may wait on container_manager's lock while a warm-up
        # thread holds it — neither should stall every other room's turn.
        target_url = await asyncio.to_thread(get_or_create_container, room_key, config)
    except Exception as exc:
        # Same guard as warmup._run_warmup's (its twin; keep in sync).
        reason = describe_error("container", exc)
        logger.error(f"Failed to get/create container for room {room_key}: {reason}")
        return AgentTurn(outcome="agent_failed", text=AGENT_FAILURE_NOTICE, error=reason)

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
            idle_timeout_seconds=config.HERMES_IDLE_TIMEOUT_SECONDS,
            max_seconds=config.HERMES_REQUEST_TIMEOUT_SECONDS,
            system=system,
        )
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        reason = describe_error("agent", exc)
        # Two different budgets ran out, and they mean different things: httpx
        # raises when the stream went silent (idle — the agent stopped even
        # sending keepalives, so it is probably dead), asyncio.timeout when the
        # whole turn passed the absolute ceiling (still alive, just far too
        # long). The room hears the same notice; the operator needs the
        # difference to know which env var to raise (docs/troubleshooting.md).
        idle = isinstance(exc, httpx.TimeoutException)
        timed_out = idle or isinstance(exc, TimeoutError)
        if timed_out:
            kind, limit = (
                ("idle", config.HERMES_IDLE_TIMEOUT_SECONDS)
                if idle
                else ("ceiling", config.HERMES_REQUEST_TIMEOUT_SECONDS)
            )
            logger.error(
                f"Hermes agent request hit the {kind} timeout for room {room_key} "
                f"after {limit}s: {reason}"
            )
        else:
            logger.error(f"Hermes agent request failed for room {room_key}: {reason}")
        return AgentTurn(
            outcome="agent_failed",
            text=AGENT_TIMEOUT_NOTICE if timed_out else AGENT_FAILURE_NOTICE,
            session_id=session_id,
            rotated=plan.rotated,
            duration_ms=elapsed_ms(started),
            error=reason,
        )

    complete_turn(config, room_key, epoch=plan.epoch, prompt_tokens=result.prompt_tokens)
    return AgentTurn(
        outcome="replied",
        text=result.text,
        session_id=session_id,
        rotated=plan.rotated,
        duration_ms=elapsed_ms(started),
        prompt_tokens=result.prompt_tokens,
        tool_calls=result.tool_calls,
        api_calls=result.api_calls,
    )
