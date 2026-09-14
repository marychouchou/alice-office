"""The per-turn envelope: what the router knows that Hermes's state.db cannot.

Conversation *content* is never copied here. Every message that actually
reaches an agent is already stored, in full and with more detail (tool calls,
tool results, reasoning, per-turn tokens and cost), in that room's
`data/<room_id>/state.db` — the single source of truth (docs/logging-design.md
§5.7). What `state.db` cannot know is everything that happened *around* the
turn: a group message that was only observed, a message the Google gate
blocked, a session rotation, an agent call that failed, a reply the room never
received. One `TurnEnvelope` per inbound message records exactly that, and
`session_id` joins it back onto `state.db.sessions.id` for the turns that did
reach the agent.

Two sinks, one call site. `record_turn` (a) emits the envelope through the
`alice.conversation` stdlib/structlog logger as the `conversation_turn` event,
so it rides the normal stdout → collector path configured in `logging_setup`,
and (b) appends the same object as one JSON line to
`DATA_DIR/_conversations/<room_key>.jsonl`.

The file write is a direct `open(..., "a")` rather than a logging FileHandler.
A handler would have to be either one per room (unbounded open file handles,
and a logger tree that grows with the deployment) or a single handler that
re-points itself per record — a stateful, racy object for what is one append of
one line. Writing the line here keeps `logging_setup` untouched and makes the
file's format independent of any log configuration.

The envelope is emitted by the *adapter*, not by core: `delivered` is only
known once the channel has tried to send, so core returns the draft
(`core.InboundResult.envelope`) and each adapter fills that one field in before
calling `record_turn`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from alice_office_router.config import Settings

logger = logging.getLogger(__name__)

# Separate from the module logger so a collector (or a local `jq`) can select
# the envelope stream on its own: {service="router"} | json | logger="alice.conversation".
_envelope_logger = structlog.stdlib.get_logger("alice.conversation")

# The structured event name every envelope line carries (docs/logging-design.md §5.4).
TURN_EVENT = "conversation_turn"

# How this file's lines are shaped. Bump only when a field changes meaning or
# disappears; adding an optional field does not need a bump, since readers
# parse with a model that defaults missing fields.
SCHEMA_VERSION = 1

Outcome = Literal["replied", "observed", "reset", "blocked", "agent_failed", "silence"]


def _now_iso() -> str:
    """Return the current UTC time in the same ISO-8601 shape log lines use.

    Returns:
        e.g. "2026-09-14T07:21:03.481922Z" — matching logging_setup's
        TimeStamper so an envelope line and its log line sort together.
    """
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


class TurnEnvelope(BaseModel):
    """One inbound message's router-side record (no reply text — see module doc).

    Attributes:
        schema_version: Format version of this record (SCHEMA_VERSION).
        ts: ISO-8601 UTC timestamp of when the turn finished routing.
        request_id: The HTTP request this turn belongs to, from the log
            contextvars; None outside a request.
        event_id: The channel's own event id (LINE `webhookEventId`), or None.
        channel: Originating adapter name ("line", "api").
        room_key: The room key core routes on — also the JSONL file's name.
        session_id: The exact X-Hermes-Session-Id this turn sent, i.e. the join
            key onto `state.db.sessions.id`. None when no agent call was made.
        outcome: How the turn ended. Only "replied" has a full `state.db`
            counterpart; the other five exist nowhere else, which is the whole
            reason this record exists.
        inbound_text: The user's text — recorded ONLY when outcome != "replied",
            because a replied turn's text is already in `state.db`.
        is_group: Whether the room holds multiple people.
        addressed: Whether the message was directed at the bot.
        sender_id: The group speaker's native id, if the channel resolved one.
        sender_name: The group speaker's display name, if resolved.
        gate_status: The Google OAuth gate's verdict ("ok"/"notice"/"blocked"),
            or None when the gate was short-circuited (observe, reset).
        rotated: Whether this turn rotated the room to a fresh session epoch.
        agent_duration_ms: Wall time of the agent HTTP call, or None if no call
            was made.
        prompt_tokens: The reply's reported prompt_tokens (see AgentReply's
            caveat — a sum across tool-loop iterations, not a context size).
        error: Short reason string when the turn failed; None otherwise.
        delivered: Whether the channel actually sent the reply — True/False, or
            None when there was nothing to deliver (observed, silence, a failed
            agent call). Filled by the adapter, never by core.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    ts: str = Field(default_factory=_now_iso)
    request_id: str | None = None
    event_id: str | None = None
    channel: str
    room_key: str
    session_id: str | None = None
    outcome: Outcome
    inbound_text: str | None = None
    is_group: bool = False
    addressed: bool = True
    sender_id: str | None = None
    sender_name: str | None = None
    gate_status: str | None = None
    rotated: bool = False
    agent_duration_ms: float | None = None
    prompt_tokens: int | None = None
    error: str | None = None
    delivered: bool | None = None


def record_turn(envelope: TurnEnvelope, config: Settings) -> None:
    """Emit one turn envelope to the log stream and (if enabled) to its JSONL file.

    The log line always goes out; the file append is skipped when
    CONVERSATION_LOG_ENABLED is False. A file error is logged and swallowed —
    a bookkeeping record must never break the turn that produced it (the reply
    has already been delivered by the time this runs).

    Args:
        envelope: The completed envelope, `delivered` already filled by the
            adapter.
        config: Application settings (the enable flag and the file path).
    """
    fields = envelope.model_dump()
    # `ts` is re-stamped by logging_setup's TimeStamper on the way out, to the
    # emit time — microseconds later, and consistent with every other log line.
    _envelope_logger.info(TURN_EVENT, **fields)

    if not config.CONVERSATION_LOG_ENABLED:
        return

    path = config.room_conversation_log(envelope.room_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{envelope.model_dump_json()}\n")
    except OSError as exc:
        logger.error(f"Failed to append conversation envelope for room {envelope.room_key}: {exc}")
