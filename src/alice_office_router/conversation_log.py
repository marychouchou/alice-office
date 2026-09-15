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

Two sinks, one call site, and **they do not carry the same fields**.
`record_turn` (a) emits the envelope's *metadata* through the
`alice.conversation` stdlib/structlog logger as the `conversation_turn` event,
so it rides the normal stdout → collector path configured in `logging_setup`,
and (b) appends the *whole* object as one JSON line to
`DATA_DIR/_conversations/<room_key>.jsonl`.

The difference is `_JSONL_ONLY_FIELDS`: `inbound_text`, `sender_id` and
`sender_name` never reach the log stream. Those three are the only message
content and the only personal identifiers an envelope holds, and the stream
leaves the room's own boundary — stdout is shipped by a collector into Loki,
where it is retained for 30 days and queryable by any operator, with no
`CONVERSATION_LOG_ENABLED` switch to turn it off (docs/logging-design.md §6).
The JSONL file is the deployment's own record, stays on the host, and *is*
gated by that flag, so it keeps them. Everything else — outcome, gate verdict,
timings, ids — is metadata and goes to both.

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

Adding a new outcome
--------------------
`Outcome` below is the single definition every other module imports; adding a
value means touching all five of these, in order:

1. `Outcome` here — the Literal itself.
2. `core._route` — the branch that actually returns the new outcome, and
   `core._TEXT_IN_STATE_DB` (does Hermes already hold this outcome's message
   text, or is this envelope the only copy?).
3. `conversation_store.UNRECORDED_OUTCOMES` — add it only if the message never
   reached Hermes, i.e. state.db holds no row for the turn.
4. `scripts/conversations.py` — `_is_missing_from_state_db` if the answer to
   (3) was conditional, and any rendering that names outcomes.
5. `docs/logging-design.md` §5.7's outcome table.
"""

from __future__ import annotations

import logging
import os
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

# Fields the JSONL file keeps and the log stream must never carry: the message
# text plus the group speaker's identity (see the module docstring for why the
# two sinks differ). Kept as data rather than a hand-written kwargs list so a
# new privacy-relevant field is one entry here, not an edit to a call site.
_JSONL_ONLY_FIELDS = frozenset({"inbound_text", "sender_id", "sender_name"})

# How this file's lines are shaped. Bump only when a field changes meaning or
# disappears; adding an optional field does not need a bump, since readers
# parse with a model that defaults missing fields.
SCHEMA_VERSION = 1

Outcome = Literal["replied", "observed", "reset", "blocked", "agent_failed", "silence"]

# The envelope file holds the only copy of a message's text that ever leaves the
# turn, for the outcomes whose message never reached Hermes. `_conversations/`
# is therefore owner-only: the rest of DATA_DIR is per-room bind mounts a
# container writes, but nothing except the router ever needs to read this.
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _now_iso() -> str:
    """Return the current UTC time in the same ISO-8601 shape log lines use.

    Returns:
        e.g. "2026-09-14T07:21:03.481922Z" — matching logging_setup's
        TimeStamper so an envelope line and its log line sort together.
    """
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


class TurnEnvelope(BaseModel):
    """One inbound message's router-side record (no reply text — see module doc).

    Three attributes below are marked "JSONL only": they are written to the
    room's envelope file but stripped from the log stream (`_JSONL_ONLY_FIELDS`).

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
        inbound_text: JSONL only (never logged). The user's text — recorded ONLY
            when outcome != "replied", because a replied turn's text is already
            in `state.db`.
        is_group: Whether the room holds multiple people.
        addressed: Whether the message was directed at the bot.
        sender_id: JSONL only (never logged). The group speaker's native id, if
            the channel resolved one.
        sender_name: JSONL only (never logged). The group speaker's display
            name, if resolved.
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
    """Emit a turn's metadata to the log stream and the whole envelope to its file.

    The log line always goes out, minus `_JSONL_ONLY_FIELDS` — the stream is
    shipped off the host and has no disable switch, so message text and speaker
    identity must not be in it. The file append carries every field and is
    skipped when CONVERSATION_LOG_ENABLED is False. A file error is logged and
    swallowed — a bookkeeping record must never break the turn that produced it
    (the reply has already been delivered by the time this runs).

    Args:
        envelope: The completed envelope, `delivered` already filled by the
            adapter.
        config: Application settings (the enable flag and the file path).
    """
    fields = {
        name: value
        for name, value in envelope.model_dump().items()
        if name not in _JSONL_ONLY_FIELDS
    }
    # `ts` is re-stamped by logging_setup's TimeStamper on the way out, to the
    # emit time — microseconds later, and consistent with every other log line.
    _envelope_logger.info(TURN_EVENT, **fields)

    if not config.CONVERSATION_LOG_ENABLED:
        return

    path = config.room_conversation_log(envelope.room_key)
    try:
        # `mode` applies only to directories this call creates, and umask can
        # only take bits away, so there is no "first time?" branch to write.
        path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        # One open-append-close per turn, and one `write()` of one line. That is
        # atomic enough for the current deployment only because it runs a SINGLE
        # uvicorn worker: O_APPEND makes concurrent writers interleave safely at
        # the syscall level, but Python may still split a long line across two
        # write() calls, and two processes would then interleave halves. If the
        # deployment ever grows to multiple workers (or a second process writing
        # these files), this sink needs a lock or one file per worker.
        #
        # os.open rather than Path.open: the 0600 must be on the file from the
        # moment it exists, and `Path.open("a")` creates it 0666 & ~umask.
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(f"{envelope.model_dump_json()}\n")
    except OSError as exc:
        logger.error(f"Failed to append conversation envelope for room {envelope.room_key}: {exc}")
