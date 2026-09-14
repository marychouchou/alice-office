"""Read-only view over the two halves of a deployment's conversation record.

Half one is each room's `data/<room_id>/state.db` — Hermes's own SQLite store,
the authoritative transcript (user/assistant/tool messages, tool calls,
reasoning, per-session tokens and cost). Half two is
`data/_conversations/<room_key>.jsonl` — the router's turn envelopes, which say
what happened to the messages that never reached the agent, and how long the
ones that did took (docs/logging-design.md §5.7).

This module only reads, and only from the filesystem: every connection is
opened `file:...?mode=ro` so nothing here can ever write to a live container's
database (WAL mode allows readers alongside Hermes's single writer). It holds
no docker, no HTTP, and no Settings dependency, so `scripts/conversations.py`
can import it without the router's environment being configured.

Schema coupling is real and deliberate (§5.8): the column names below track
Hermes's `state.db`. `check_schema_version` reports anything outside the tested
range so a future upstream change surfaces as a warning rather than a silently
wrong read.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from alice_office_router.conversation_log import TurnEnvelope

# The Hermes state.db schema versions this module's queries were written
# against: 20 is what this deployment runs, 23 what upstream documents. Columns
# have only ever been added between them, so anything in range reads correctly.
SCHEMA_VERSION_MIN = 20
SCHEMA_VERSION_MAX = 23

# fts5's trigram tokenizer cannot match a pattern shorter than one trigram, so
# a 1-2 character term (common in Chinese: 「天氣」) silently returns nothing.
# Such terms go through a LIKE scan instead — slower, but correct.
_TRIGRAM_MIN_CHARS = 3

_STATE_DB = "state.db"

# An envelope is stamped when its turn FINISHED, so it always lands after the
# user message that started it — by the agent call's duration (the client's own
# timeout caps that at 120s) plus a handoff summary on a rotating turn. 300s
# covers the worst case with margin; beyond it, the nearest envelope is a
# different turn and no envelope is the honest answer.
NEAREST_TOLERANCE_SECONDS = 300.0

# The outcomes that leave NO row in state.db: the message never reached the
# agent. Only these are rendered from the envelope alone — "agent_failed" and
# "silence" did reach Hermes, which recorded the user message itself.
UNRECORDED_OUTCOMES = frozenset({"observed", "reset", "blocked"})

_MESSAGE_COLUMNS = (
    "id, session_id, role, content, tool_name, tool_calls, "
    "timestamp, token_count, finish_reason, reasoning, reasoning_content"
)

# Hermes's context compaction re-inserts the messages it compacts, so the same
# turn can appear several times with an identical timestamp. Keeping the lowest
# id of each group gives one row per real message while preserving the
# pre-compaction history that `active = 1` alone would drop.
#
# The group must be keyed on the message's CONTENT, not just its coordinates.
# Measured against this deployment's own state.db (2026-09-14, schema 20): every
# duplicate group holds byte-identical rows, and `compacted`/`active` differ
# WITHIN a group (e.g. ids 30/42/50 carry compacted=1, id 56 the same row with
# compacted=0, active=1) — so neither column can discriminate here; grouping by
# either would un-deduplicate the very rows this clause exists to merge.
# Meanwhile (session, role, timestamp, tool_name) alone silently merges rows
# that are genuinely distinct: two parallel tool results returning in the same
# tick, or two user messages inside the same second. A content prefix plus
# `tool_call_id` separates those while still collapsing the compaction copies.
# 64 chars is enough to tell real messages apart without indexing whole tool
# payloads (some are tens of KB).
_DEDUPE = (
    "id IN (SELECT MIN(id) FROM messages GROUP BY session_id, role, timestamp, tool_name, "
    "COALESCE(tool_call_id, ''), COALESCE(substr(content, 1, 64), ''))"
)

_SESSION_COLUMNS = (
    "id, model, started_at, ended_at, end_reason, message_count, tool_call_count, "
    "input_tokens, output_tokens, estimated_cost_usd, title, api_call_count"
)


@dataclass(frozen=True)
class MessageRow:
    """One row of a room's `messages` table.

    Attributes:
        id: The row's primary key (also its `messages_fts_trigram` rowid).
        session_id: The session this message belongs to.
        role: "user", "assistant" or "tool".
        content: The message text; empty for an assistant turn that only made
            tool calls.
        tool_name: The tool a `tool` row is the result of, else None.
        tool_calls: Raw JSON of an assistant turn's tool calls, else None.
        timestamp: Unix epoch seconds.
        token_count: Hermes's own count for this message, if recorded.
        finish_reason: The completion's finish reason, if recorded.
        reasoning: The model's reasoning text, if the model emitted any.
    """

    id: int
    session_id: str
    role: str
    content: str
    tool_name: str | None
    tool_calls: str | None
    timestamp: float
    token_count: int | None
    finish_reason: str | None
    reasoning: str | None


@dataclass(frozen=True)
class SessionRow:
    """One row of a room's `sessions` table.

    Attributes:
        id: The session id — equal to the X-Hermes-Session-Id the router sent,
            i.e. the join key onto a turn envelope's `session_id`.
        model: The LLM this session ran under.
        started_at: Unix epoch seconds of the session's first message.
        ended_at: Unix epoch seconds of its close, or None while open.
        end_reason: Why it closed, if it did.
        message_count: Messages recorded in the session.
        tool_call_count: Tool calls made during it.
        input_tokens: Prompt tokens billed across the session.
        output_tokens: Completion tokens billed across the session.
        estimated_cost_usd: Hermes's own cost estimate.
        title: The session's auto-generated title, if any.
        api_call_count: Completion requests made during it.
    """

    id: str
    model: str | None
    started_at: float
    ended_at: float | None
    end_reason: str | None
    message_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    title: str | None
    api_call_count: int


@dataclass(frozen=True)
class RoomSummary:
    """One room's roll-up, as the `rooms` subcommand prints it.

    Attributes:
        room_id: The room's directory name under DATA_DIR (also its room_key).
        schema_version: The room's state.db schema version, or None if absent.
        session_count: Sessions recorded for the room.
        message_count: Messages across all of them.
        last_activity: Unix epoch seconds of the newest message, or None.
        input_tokens: Prompt tokens across all sessions.
        output_tokens: Completion tokens across all sessions.
        estimated_cost_usd: Summed cost estimate.
        envelope_count: Turn envelopes the router recorded for the room.
    """

    room_id: str
    schema_version: int | None
    session_count: int
    message_count: int
    last_activity: float | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    envelope_count: int


@dataclass
class EnvelopeIndex:
    """A room's turn envelopes, indexed for the nearest-timestamp join.

    A turn envelope is stamped when the turn *finished*, so it always lands
    after the message that started it; `nearest` therefore only ever looks
    forward in time (see its docstring), which is unambiguous as long as a
    room's turns don't overlap (single-worker deployment — see group_context).

    Attributes:
        envelopes: Every envelope read, in file order.
        by_session: session_id -> its envelopes sorted by timestamp.
    """

    envelopes: list[TurnEnvelope] = field(default_factory=list)
    by_session: dict[str, list[tuple[float, TurnEnvelope]]] = field(default_factory=dict)

    def nearest(
        self, session_id: str, ts: float, *, tolerance: float = NEAREST_TOLERANCE_SECONDS
    ) -> TurnEnvelope | None:
        """Find the envelope of the turn a message at `ts` belongs to.

        The search is forward-only: a turn's envelope is stamped when that turn
        ENDED, so the envelope belonging to a message is always the first one
        at or after it. Taking the nearest in either direction gets this wrong
        for exactly the case that matters — back-to-back turns. A 30-second
        turn's envelope lands 30s after its own user message but only a few
        seconds before the NEXT one, so the next message would be labelled with
        the previous turn's outcome and latency, and the last message of a room
        would inherit an envelope it has nothing to do with.

        Args:
            session_id: The message's session id.
            ts: The message's unix timestamp.
            tolerance: Maximum gap to accept, in seconds. Without it the first
                envelope after a message in a long-lived session could be hours
                later and describe an entirely different turn — every message in
                a room that has one envelope would be labelled with it.

        Returns:
            The earliest envelope in that session at or after `ts` and within
            `tolerance`, or None — which is also what a turn from before this
            feature, a turn whose envelope was never written, or a room whose
            envelopes were disabled, correctly reads as.
        """
        candidates = self.by_session.get(session_id)
        if not candidates:
            return None
        # by_session is sorted ascending, so the first hit is the earliest.
        for envelope_ts, envelope in candidates:
            if envelope_ts >= ts:
                return envelope if envelope_ts - ts <= tolerance else None
        return None


def _as_str(value: object) -> str:
    """Coerce a SQLite cell to a string, mapping NULL to the empty string.

    Args:
        value: The raw cell value.

    Returns:
        The string, or "" for NULL or an unexpected type.
    """
    return value if isinstance(value, str) else ""


def _as_opt_str(value: object) -> str | None:
    """Coerce a SQLite cell to an optional string.

    Args:
        value: The raw cell value.

    Returns:
        The string, or None for NULL or an unexpected type.
    """
    return value if isinstance(value, str) else None


def _as_float(value: object) -> float:
    """Coerce a SQLite cell to a float, mapping NULL to 0.0.

    Args:
        value: The raw cell value.

    Returns:
        The number as a float, or 0.0 for NULL or an unexpected type.
    """
    return float(value) if isinstance(value, int | float) else 0.0


def _as_opt_float(value: object) -> float | None:
    """Coerce a SQLite cell to an optional float.

    Args:
        value: The raw cell value.

    Returns:
        The number as a float, or None for NULL or an unexpected type.
    """
    return float(value) if isinstance(value, int | float) else None


def _as_int(value: object) -> int:
    """Coerce a SQLite cell to an int, mapping NULL to 0.

    Args:
        value: The raw cell value.

    Returns:
        The number as an int, or 0 for NULL or an unexpected type.
    """
    return int(value) if isinstance(value, int | float) else 0


def _as_opt_int(value: object) -> int | None:
    """Coerce a SQLite cell to an optional int.

    Args:
        value: The raw cell value.

    Returns:
        The number as an int, or None for NULL or an unexpected type.
    """
    return int(value) if isinstance(value, int | float) else None


def state_db_path(data_dir: Path, room_id: str) -> Path:
    """Return a room's Hermes state.db path.

    Args:
        data_dir: The deployment's DATA_DIR.
        room_id: The room's directory name.

    Returns:
        data_dir / room_id / "state.db" — which may not exist yet (a room whose
        container has never booted).
    """
    return data_dir / room_id / _STATE_DB


def list_room_ids(data_dir: Path) -> list[str]:
    """List the rooms that have a Hermes database, newest name order aside.

    Args:
        data_dir: The deployment's DATA_DIR.

    Returns:
        Sorted room directory names. Underscore-prefixed directories
        (`_google`, `_conversations`) are deployment-level, not rooms, and
        directories without a state.db have no transcript to read.
    """
    if not data_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in data_dir.iterdir()
        if entry.is_dir() and not entry.name.startswith("_") and (entry / _STATE_DB).exists()
    )


def connect_state_db(path: Path) -> sqlite3.Connection:
    """Open a room's state.db strictly read-only (caller closes it).

    Args:
        path: Path to the room's state.db.

    Returns:
        A connection opened through the `file:...?mode=ro` URI, so neither this
        process nor a bug in it can write to a database a live container owns.
        WAL mode lets this coexist with the container's single writer.

    Raises:
        sqlite3.Error: If the file is missing or not a database.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def open_state_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a room's state.db read-only for the duration of a `with` block.

    Args:
        path: Path to the room's state.db.

    Yields:
        The read-only connection from connect_state_db, closed on exit.

    Raises:
        sqlite3.Error: If the file is missing or not a database.
    """
    connection = connect_state_db(path)
    try:
        yield connection
    finally:
        connection.close()


def check_schema_version(connection: sqlite3.Connection) -> tuple[int | None, str | None]:
    """Read a state.db's schema version and flag anything untested.

    Args:
        connection: An open read-only connection.

    Returns:
        (version, warning). The warning is None when the version is inside the
        tested range; otherwise it says so, for the caller to print to stderr.
        A database with no schema_version table at all reads as (None, warning).
    """
    try:
        row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return None, f"state.db has no readable schema_version ({exc}); columns may have moved"
    version = _as_opt_int(row["version"]) if row is not None else None
    if version is None:
        return None, "state.db has an empty schema_version table; columns may have moved"
    if version < SCHEMA_VERSION_MIN or version > SCHEMA_VERSION_MAX:
        return version, (
            f"state.db schema_version {version} is outside the tested range "
            f"{SCHEMA_VERSION_MIN}-{SCHEMA_VERSION_MAX}; output may be incomplete"
        )
    return version, None


def _to_message(row: sqlite3.Row) -> MessageRow:
    """Build a MessageRow from a `messages` result row.

    Args:
        row: A row selected with _MESSAGE_COLUMNS.

    Returns:
        The typed MessageRow. `reasoning` falls back to `reasoning_content`,
        which is where some providers put the same text.
    """
    return MessageRow(
        id=_as_int(row["id"]),
        session_id=_as_str(row["session_id"]),
        role=_as_str(row["role"]),
        content=_as_str(row["content"]),
        tool_name=_as_opt_str(row["tool_name"]),
        tool_calls=_as_opt_str(row["tool_calls"]),
        timestamp=_as_float(row["timestamp"]),
        token_count=_as_opt_int(row["token_count"]),
        finish_reason=_as_opt_str(row["finish_reason"]),
        reasoning=_as_opt_str(row["reasoning"]) or _as_opt_str(row["reasoning_content"]),
    )


def _to_session(row: sqlite3.Row) -> SessionRow:
    """Build a SessionRow from a `sessions` result row.

    Args:
        row: A row selected with _SESSION_COLUMNS.

    Returns:
        The typed SessionRow.
    """
    return SessionRow(
        id=_as_str(row["id"]),
        model=_as_opt_str(row["model"]),
        started_at=_as_float(row["started_at"]),
        ended_at=_as_opt_float(row["ended_at"]),
        end_reason=_as_opt_str(row["end_reason"]),
        message_count=_as_int(row["message_count"]),
        tool_call_count=_as_int(row["tool_call_count"]),
        input_tokens=_as_int(row["input_tokens"]),
        output_tokens=_as_int(row["output_tokens"]),
        estimated_cost_usd=_as_float(row["estimated_cost_usd"]),
        title=_as_opt_str(row["title"]),
        api_call_count=_as_int(row["api_call_count"]),
    )


def read_sessions(
    connection: sqlite3.Connection, *, since: float | None = None
) -> list[SessionRow]:
    """Read a room's sessions, oldest first.

    Args:
        connection: An open read-only connection.
        since: Only sessions whose last activity is at or after this unix
            timestamp; None for all of them.

    Returns:
        The room's SessionRows ordered by start time.
    """
    sql = f"SELECT {_SESSION_COLUMNS} FROM sessions"
    params: list[float] = []
    if since is not None:
        sql += " WHERE COALESCE(ended_at, started_at) >= ?"
        params.append(since)
    sql += " ORDER BY started_at"
    return [_to_session(row) for row in connection.execute(sql, params).fetchall()]


def read_messages(
    connection: sqlite3.Connection, *, session_id: str | None = None, since: float | None = None
) -> list[MessageRow]:
    """Read a room's messages in transcript order.

    Args:
        connection: An open read-only connection.
        session_id: Restrict to one session; None reads every session.
        since: Only messages at or after this unix timestamp; None for all.

    Returns:
        MessageRows ordered by timestamp then id, i.e. the order they were
        written, so an assistant turn's tool calls precede their results.
    """
    clauses: list[str] = [_DEDUPE]
    params: list[str | float] = []
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if since is not None:
        clauses.append("timestamp >= ?")
        params.append(since)
    where = " AND ".join(clauses)
    sql = f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {where} ORDER BY timestamp, id"
    return [_to_message(row) for row in connection.execute(sql, params).fetchall()]


def _search_like(connection: sqlite3.Connection, term: str, limit: int) -> list[MessageRow]:
    """Scan `messages` for a literal substring, newest first.

    Args:
        connection: An open read-only connection.
        term: The text to look for; its LIKE metacharacters are escaped, so it
            matches literally.
        limit: Maximum rows to return.

    Returns:
        Matching MessageRows, newest first.
    """
    sql = (
        f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {_DEDUPE} AND content LIKE ? "
        "ESCAPE '\\' ORDER BY timestamp DESC LIMIT ?"
    )
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return [_to_message(row) for row in connection.execute(sql, (f"%{escaped}%", limit)).fetchall()]


def search_messages(
    connection: sqlite3.Connection, term: str, *, limit: int = 50
) -> list[MessageRow]:
    """Full-text search one room's messages, newest first.

    Uses the `messages_fts_trigram` index, which is what makes Chinese search
    work at all (the default tokenizer does not segment CJK). Two cases fall
    back to a plain LIKE scan, which returns the same rows more slowly: terms
    shorter than one trigram (fts5's trigram tokenizer silently matches nothing
    for them), and a database that has no `messages_fts_trigram` table at all
    (an older or hand-built state.db). Normalizing the second case here is what
    keeps every caller free of a "does this room have the index?" branch — the
    only alternative is a search that aborts a whole cross-room loop because one
    room's schema is older.

    Args:
        connection: An open read-only connection.
        term: The text to look for; matched as a literal phrase, so fts5
            operators inside it are not interpreted.
        limit: Maximum rows to return.

    Returns:
        Matching MessageRows, newest first.

    Raises:
        sqlite3.Error: If the `messages` table itself cannot be read — a
            database this module has no way to answer from, which the caller
            reports per room rather than treating as "no matches".
    """
    if len(term) < _TRIGRAM_MIN_CHARS:
        return _search_like(connection, term, limit)

    sql = (
        f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {_DEDUPE} AND id IN "
        "(SELECT rowid FROM messages_fts_trigram WHERE messages_fts_trigram MATCH ?) "
        "ORDER BY timestamp DESC LIMIT ?"
    )
    phrase = '"' + term.replace('"', '""') + '"'
    try:
        rows = connection.execute(sql, (phrase, limit)).fetchall()
    except sqlite3.Error:
        return _search_like(connection, term, limit)
    return [_to_message(row) for row in rows]


def read_envelopes(conversations_dir: Path, room_id: str) -> EnvelopeIndex:
    """Read and index one room's turn envelopes.

    A missing file, a blank line, or a line written by a future schema is
    normalized away here (the unreadable line is skipped), so every caller sees
    the same thing: an index that may simply be empty.

    Args:
        conversations_dir: DATA_DIR / "_conversations".
        room_id: The room whose <room_id>.jsonl to read.

    Returns:
        An EnvelopeIndex; empty when the room has no envelope file.
    """
    index = EnvelopeIndex()
    path = conversations_dir / f"{room_id}.jsonl"
    if not path.exists():
        return index
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            envelope = TurnEnvelope.model_validate_json(line)
        except ValidationError:
            continue
        index.envelopes.append(envelope)
        if envelope.session_id:
            index.by_session.setdefault(envelope.session_id, []).append(
                (parse_iso(envelope.ts), envelope)
            )
    for entries in index.by_session.values():
        entries.sort(key=lambda item: item[0])
    return index


def parse_iso(value: str) -> float:
    """Convert an envelope's ISO-8601 timestamp to unix seconds.

    Args:
        value: The `ts` field, e.g. "2026-09-14T07:21:03.481922Z".

    Returns:
        Unix epoch seconds, or 0.0 when the string cannot be parsed (an
        envelope written by something else) — which just makes that envelope
        the worst nearest-timestamp candidate instead of raising.
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_since(value: str) -> float:
    """Parse a `--since` argument into a unix timestamp.

    Args:
        value: Either a relative window (`7d`, `36h`, `90m`) or an absolute
            UTC date (`2026-09-01`).

    Returns:
        The unix timestamp the window starts at.

    Raises:
        ValueError: If the string is neither shape.
    """
    units = {"d": 86400.0, "h": 3600.0, "m": 60.0}
    if len(value) > 1 and value[-1] in units and value[:-1].isdigit():
        return datetime.now(tz=UTC).timestamp() - int(value[:-1]) * units[value[-1]]
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()


def short_sender(sender_id: str | None) -> str | None:
    """Pseudonymize a sender id for export.

    Args:
        sender_id: The channel's native speaker id, or None.

    Returns:
        The first 8 hex digits of its SHA-256, stable across exports so the
        same person stays recognizable without the id leaving the deployment;
        None passes through.
    """
    if sender_id is None:
        return None
    return sha256(sender_id.encode("utf-8")).hexdigest()[:8]


def format_ts(ts: float) -> str:
    """Render a unix timestamp the way the CLI and exports print it.

    Args:
        ts: Unix epoch seconds.

    Returns:
        "YYYY-MM-DD HH:MM:SS" in UTC, or "-" for a missing/zero stamp.
    """
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def summarize_tool_calls(raw: str | None) -> str:
    """Reduce an assistant turn's raw `tool_calls` JSON to a one-line summary.

    Args:
        raw: The column's JSON text, or None.

    Returns:
        A comma-separated list of the called function names, the raw text when
        it isn't the expected shape, or "" when there is nothing.
    """
    if not raw:
        return ""
    try:
        calls = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(calls, list):
        return raw
    names = [
        str(call.get("function", {}).get("name", "?")) for call in calls if isinstance(call, dict)
    ]
    return ", ".join(names)


def room_summary(data_dir: Path, conversations_dir: Path, room_id: str) -> RoomSummary:
    """Roll one room up into the line the `rooms` subcommand prints.

    Args:
        data_dir: The deployment's DATA_DIR.
        conversations_dir: DATA_DIR / "_conversations".
        room_id: The room to summarize.

    Returns:
        The room's RoomSummary, with envelope_count read from the router's own
        record (which exists even for rooms whose agent never answered).
    """
    envelopes = read_envelopes(conversations_dir, room_id)
    with open_state_db(state_db_path(data_dir, room_id)) as connection:
        version, _ = check_schema_version(connection)
        sessions = read_sessions(connection)
        row = connection.execute(
            f"SELECT MAX(timestamp), COUNT(*) FROM messages WHERE {_DEDUPE}"
        ).fetchone()
    return RoomSummary(
        room_id=room_id,
        schema_version=version,
        session_count=len(sessions),
        message_count=_as_int(row[1]) if row is not None else 0,
        last_activity=_as_opt_float(row[0]) if row is not None else None,
        input_tokens=sum(session.input_tokens for session in sessions),
        output_tokens=sum(session.output_tokens for session in sessions),
        estimated_cost_usd=sum(session.estimated_cost_usd for session in sessions),
        envelope_count=len(envelopes.envelopes),
    )


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Return the nearest-rank percentile of a sample.

    Args:
        values: The sample; may be empty.
        fraction: The percentile as a fraction, e.g. 0.95.

    Returns:
        The value at that rank, or None for an empty sample.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[rank]
