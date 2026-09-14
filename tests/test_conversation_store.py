from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alice_office_router.conversation_log import TurnEnvelope
from alice_office_router.conversation_store import (
    SCHEMA_VERSION_MAX,
    MessageRow,
    check_schema_version,
    connect_state_db,
    list_room_ids,
    parse_since,
    read_envelopes,
    read_messages,
    read_sessions,
    room_summary,
    search_messages,
    short_sender,
    summarize_tool_calls,
)

# scripts/ is not an importable package, so load the CLI by file path (same
# pattern as tests/test_debug_room.py).
_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "conversations.py"
_spec = importlib.util.spec_from_file_location("conversations", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
conversations = importlib.util.module_from_spec(_spec)
# The module must be in sys.modules before it runs: @dataclass resolves its
# field types through sys.modules[cls.__module__].
sys.modules[_spec.name] = conversations
_spec.loader.exec_module(conversations)

_ROOM = "line_room_AAA"
_SESSION = "line_room_AAA#1"

# The columns this repo reads; a fixture that carries only these is enough,
# since every query names its columns explicitly (conversation_store).
_SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, ended_at REAL,
    end_reason TEXT, message_count INTEGER, tool_call_count INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL,
    title TEXT, api_call_count INTEGER
)
"""

_MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL,
    token_count INTEGER, finish_reason TEXT, reasoning TEXT, reasoning_content TEXT,
    active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0
)
"""

_TOOL_CALLS_JSON = json.dumps(
    [{"id": "c1", "type": "function", "function": {"name": "drive_list_files", "arguments": "{}"}}]
)

# (role, content, tool_name, tool_calls, timestamp, reasoning)
_ROWS: list[tuple[str, str, str | None, str | None, float, str | None]] = [
    ("user", "我的行事曆上有什麼會議", None, None, 1_000.0, None),
    ("assistant", "", None, _TOOL_CALLS_JSON, 1_002.0, "先查一下行事曆"),
    ("tool", "x" * 900, "drive_list_files", None, 1_003.0, None),
    ("assistant", "今天下午三點有一場產品會議。", None, None, 1_005.0, None),
    ("user", "幫我改到四點", None, None, 1_100.0, None),
    ("assistant", "已經改到四點了。", None, None, 1_104.0, None),
]


def _build_state_db(path: Path) -> None:
    """Write a minimal Hermes-shaped state.db (sessions + messages + fts5 trigram).

    Args:
        path: Where to create the database file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER)")
        connection.execute("INSERT INTO schema_version VALUES (20)")
        connection.execute(_SESSIONS_DDL)
        connection.execute(_MESSAGES_DDL)
        connection.execute(
            "CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram')"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _SESSION,
                "api_server",
                "hermes-4",
                1_000.0,
                None,
                None,
                6,
                1,
                5000,
                300,
                0.0123,
                "行事曆",
                3,
            ),
        )
        for role, content, tool_name, tool_calls, ts, reasoning in _ROWS:
            cursor = connection.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                "timestamp, reasoning) VALUES (?,?,?,?,?,?,?)",
                (_SESSION, role, content, tool_calls, tool_name, ts, reasoning),
            )
            connection.execute(
                "INSERT INTO messages_fts_trigram (rowid, content) VALUES (?, ?)",
                (cursor.lastrowid, content),
            )
    connection.close()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Build a DATA_DIR holding one room's state.db plus its turn envelopes.

    Args:
        tmp_path: pytest's per-test temp directory.

    Returns:
        The DATA_DIR path, with data/<room>/state.db and
        data/_conversations/<room>.jsonl populated.
    """
    root = tmp_path / "data"
    _build_state_db(root / _ROOM / "state.db")
    # Deployment-level directories, and a room whose container never booted:
    # neither may show up as a room.
    (root / "_google").mkdir(parents=True, exist_ok=True)
    (root / "line_no_db").mkdir(parents=True, exist_ok=True)

    envelopes = [
        TurnEnvelope(
            ts="1970-01-01T00:16:46Z",
            channel="line",
            room_key=_ROOM,
            session_id=_SESSION,
            outcome="replied",
            agent_duration_ms=5000.0,
            delivered=True,
        ),
        TurnEnvelope(
            ts="1970-01-01T00:20:00Z",
            channel="line",
            room_key=_ROOM,
            outcome="blocked",
            inbound_text="沒授權的訊息",
            gate_status="blocked",
            delivered=True,
        ),
        TurnEnvelope(
            ts="1970-01-01T00:18:25Z",
            channel="line",
            room_key=_ROOM,
            session_id=_SESSION,
            outcome="agent_failed",
            inbound_text="幫我改到四點",
            agent_duration_ms=120_000.0,
            error="agent: timeout",
        ),
    ]
    conversations_dir = root / "_conversations"
    conversations_dir.mkdir(parents=True, exist_ok=True)
    (conversations_dir / f"{_ROOM}.jsonl").write_text(
        "\n".join(envelope.model_dump_json() for envelope in envelopes) + "\n",
        encoding="utf-8",
    )
    return root


def _user(message_id: int, ts: float, session_id: str = _SESSION) -> MessageRow:
    """A minimal `role='user'` MessageRow — all `bind_messages` looks at."""
    return MessageRow(
        id=message_id,
        session_id=session_id,
        role="user",
        content=f"m{message_id}",
        tool_name=None,
        tool_calls=None,
        timestamp=ts,
        token_count=None,
        finish_reason=None,
        reasoning=None,
    )


def _write_envelopes(conversations_dir: Path, envelopes: list[TurnEnvelope]) -> None:
    """Write a room's envelope file from TurnEnvelope objects."""
    conversations_dir.mkdir(parents=True, exist_ok=True)
    (conversations_dir / f"{_ROOM}.jsonl").write_text(
        "\n".join(envelope.model_dump_json() for envelope in envelopes) + "\n", encoding="utf-8"
    )


def _run(data_dir: Path, argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """Run the CLI against a temp DATA_DIR and return its stdout."""
    assert conversations.main(["--data-dir", str(data_dir), *argv]) == 0
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# Store — room discovery and schema guard
# ---------------------------------------------------------------------------


def test_list_room_ids_skips_underscore_dirs_and_rooms_without_a_db(data_dir: Path) -> None:
    """Only directories with a state.db, and never deployment-level `_` dirs."""
    assert list_room_ids(data_dir) == [_ROOM]


def test_schema_version_in_range_produces_no_warning(data_dir: Path) -> None:
    """The tested range (20-23) reads clean."""
    connection = connect_state_db(data_dir / _ROOM / "state.db")
    try:
        assert check_schema_version(connection) == (20, None)
    finally:
        connection.close()


def test_schema_version_above_tested_range_warns(data_dir: Path) -> None:
    """An untested upstream schema warns instead of silently misreading."""
    path = data_dir / _ROOM / "state.db"
    writable = sqlite3.connect(path)
    with writable:
        writable.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION_MAX + 5,))
    writable.close()

    connection = connect_state_db(path)
    try:
        version, warning = check_schema_version(connection)
    finally:
        connection.close()

    assert version == SCHEMA_VERSION_MAX + 5
    assert warning is not None and "outside the tested range" in warning


# ---------------------------------------------------------------------------
# Store — reads
# ---------------------------------------------------------------------------


def test_read_messages_returns_transcript_order(data_dir: Path) -> None:
    """Messages come back oldest first, tool rows included (the caller filters)."""
    connection = connect_state_db(data_dir / _ROOM / "state.db")
    try:
        messages = read_messages(connection)
        sessions = read_sessions(connection)
    finally:
        connection.close()

    assert [message.role for message in messages] == [row[0] for row in _ROWS]
    assert messages[0].timestamp == 1_000.0
    assert sessions[0].id == _SESSION
    assert sessions[0].input_tokens == 5000


def test_read_messages_filters_by_session_and_since(data_dir: Path) -> None:
    """--session and --since narrow the read at the SQL level."""
    connection = connect_state_db(data_dir / _ROOM / "state.db")
    try:
        assert read_messages(connection, session_id="nope") == []
        recent = read_messages(connection, since=1_100.0)
    finally:
        connection.close()

    assert [message.content for message in recent] == ["幫我改到四點", "已經改到四點了。"]


def test_search_finds_a_cjk_term_through_the_trigram_index(data_dir: Path) -> None:
    """A 3+ character Chinese term matches through messages_fts_trigram."""
    connection = connect_state_db(data_dir / _ROOM / "state.db")
    try:
        hits = search_messages(connection, "有什麼")
        short = search_messages(connection, "四點")
    finally:
        connection.close()

    assert [hit.role for hit in hits] == ["user"]
    assert "行事曆" in hits[0].content
    # Shorter than one trigram — the LIKE fallback still finds it.
    assert len(short) == 2


def test_room_summary_rolls_up_sessions_and_envelopes(data_dir: Path) -> None:
    """The `rooms` line combines state.db usage with the router's envelope count."""
    summary = room_summary(data_dir, data_dir / "_conversations", _ROOM)

    assert summary.session_count == 1
    assert summary.message_count == len(_ROWS)
    assert summary.envelope_count == 3
    assert summary.input_tokens == 5000
    assert summary.last_activity == 1_104.0


def test_envelopes_bind_one_to_one_within_the_tolerance(data_dir: Path) -> None:
    """Each envelope claims exactly one user message, and only a nearby one."""
    index = read_envelopes(data_dir / "_conversations", _ROOM)

    assert len(index.envelopes) == 3
    bindings = index.bind_messages([_user(1, 1_000.0), _user(5, 1_100.0)])

    assert bindings[1].outcome == "replied"
    assert bindings[5].outcome == "agent_failed"
    # The "blocked" envelope carries no session_id, so it joins onto nothing.
    assert len(index.bound) == 2

    # A message far outside the window, and one in a session no envelope names:
    # better no envelope than a wrong one.
    assert index.bind_messages([_user(1, -99_999.0)]) == {}
    assert index.bind_messages([_user(1, 1_000.0, session_id="other")]) == {}


def test_helpers_normalize_their_edge_cases() -> None:
    """Small pure helpers the CLI leans on."""
    assert summarize_tool_calls(None) == ""
    assert summarize_tool_calls(_TOOL_CALLS_JSON) == "drive_list_files"
    assert summarize_tool_calls("not json") == "not json"
    assert short_sender(None) is None
    sender = short_sender("U1")
    assert sender is not None and len(sender) == 8
    assert parse_since("2026-09-01") < parse_since("7d")


# ---------------------------------------------------------------------------
# CLI — rooms / show / search / export / stats
# ---------------------------------------------------------------------------


def test_cli_rooms_lists_the_room(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`rooms` prints one line per room with its usage roll-up."""
    out = _run(data_dir, ["rooms"], capsys)

    assert _ROOM in out
    assert "5000" in out


def test_cli_show_prints_user_and_assistant_only_by_default(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tool results and reasoning stay out unless explicitly asked for."""
    out = _run(data_dir, ["show", _ROOM], capsys)

    assert "我的行事曆上有什麼會議" in out
    assert "今天下午三點有一場產品會議。" in out
    assert "drive_list_files" not in out
    assert "先查一下行事曆" not in out
    # The envelope's outcome/latency rides on the turn it belongs to.
    assert "(replied, 5000ms)" in out


def test_cli_show_includes_turns_that_never_reached_the_agent(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blocked turn exists only in the envelope — and must still appear."""
    out = _run(data_dir, ["show", _ROOM], capsys)

    assert "沒授權的訊息" in out
    assert "(blocked)" in out
    # An agent_failed turn DID reach Hermes, so it is rendered from state.db
    # once — never twice.
    assert out.count("幫我改到四點") == 1


def test_cli_show_with_tools_and_reasoning_opts_in(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--with-tools adds the calls and (truncated) results; --with-reasoning the reasoning."""
    out = _run(data_dir, ["show", _ROOM, "--with-tools", "--with-reasoning"], capsys)

    assert "**assistant → tools** drive_list_files" in out
    assert "**tool** `drive_list_files`" in out
    assert "先查一下行事曆" in out
    assert "+400 chars" in out  # the 900-char tool result was truncated to 500


def test_cli_search_matches_across_rooms(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`search` finds a CJK term and reports the room it came from."""
    out = _run(data_dir, ["search", "有什麼"], capsys)

    assert _ROOM in out
    assert "我的行事曆上有什麼會議" in out


def test_cli_export_md_writes_front_matter_and_turns(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`export --format md` produces a file Claude Code can read directly."""
    out_dir = tmp_path / "export"
    _run(data_dir, ["export", "--room", _ROOM, "--format", "md", "--out", str(out_dir)], capsys)

    body = (out_dir / f"{_ROOM}.md").read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert f"room: {_ROOM}" in body
    assert "session_count: 1" in body
    assert "exported_at:" in body
    assert "— user" in body
    assert "**assistant**" in body
    assert "drive_list_files" not in body


def test_cli_export_jsonl_is_one_record_per_message(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`export --format jsonl` writes parseable records carrying the envelope outcome."""
    out_dir = tmp_path / "export"
    _run(data_dir, ["export", "--room", _ROOM, "--format", "jsonl", "--out", str(out_dir)], capsys)

    records = [
        json.loads(line)
        for line in (out_dir / f"{_ROOM}.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["role"] for record in records] == [
        "user",
        "assistant",
        "assistant",
        "user",
        "assistant",
    ]
    assert records[0]["outcome"] == "replied"


def test_cli_export_hashes_sender_ids_unless_raw(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A group speaker's native id is pseudonymized by default."""
    envelope = TurnEnvelope(
        ts="1970-01-01T00:16:46Z",
        channel="line",
        room_key=_ROOM,
        session_id=_SESSION,
        outcome="replied",
        is_group=True,
        sender_id="U1234",
        sender_name="王小明",
    )
    path = data_dir / "_conversations" / f"{_ROOM}.jsonl"
    path.write_text(envelope.model_dump_json() + "\n", encoding="utf-8")

    hashed = _run(data_dir, ["show", _ROOM], capsys)
    raw = _run(data_dir, ["--raw", "show", _ROOM], capsys)

    assert "U1234" not in hashed
    assert short_sender("U1234") in hashed
    assert "U1234" in raw


def test_cli_stats_reports_outcomes_and_latency(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`stats` summarizes outcomes, the failure rate, and latency percentiles."""
    out = _run(data_dir, ["stats"], capsys)

    assert "turns: 3" in out
    assert "agent_failed" in out
    assert "agent_failed rate: 33.33%" in out
    assert "p50=" in out
    assert "rooms that replied after a blocked turn: 0" in out


def test_cli_stats_counts_a_room_that_recovered_after_a_block(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A room that was blocked and later replied is the signal operators want."""
    path = data_dir / "_conversations" / f"{_ROOM}.jsonl"
    envelopes = [
        TurnEnvelope(ts="1970-01-01T00:16:40Z", channel="line", room_key=_ROOM, outcome="blocked"),
        TurnEnvelope(
            ts="1970-01-01T00:16:50Z",
            channel="line",
            room_key=_ROOM,
            session_id=_SESSION,
            outcome="replied",
        ),
    ]
    path.write_text(
        "\n".join(envelope.model_dump_json() for envelope in envelopes) + "\n", encoding="utf-8"
    )

    out = _run(data_dir, ["stats"], capsys)

    assert "rooms that replied after a blocked turn: 1" in out


def test_cli_show_warns_on_an_unknown_room(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A room with no state.db is a warning plus a non-zero exit, not a traceback."""
    assert conversations.main(["--data-dir", str(data_dir), "show", "line_nope"]) == 1
    assert "has no state.db" in capsys.readouterr().err


def test_back_to_back_turns_each_get_their_own_envelope(tmp_path: Path) -> None:
    """An envelope is stamped at turn END, so it belongs to the message BEFORE it.

    Two turns back to back — a 12s one then a 32s one. Turn 1's envelope lands
    only 8s before turn 2's user message but 12s after its own, so a
    nearest-in-either-direction join would label turn 2 with turn 1's outcome.
    """
    conversations_dir = tmp_path / "_conversations"
    _write_envelopes(
        conversations_dir,
        [
            # turn 1: user speaks at t=0, replied 12s later.
            TurnEnvelope(
                ts="1970-01-01T00:00:12Z",
                channel="line",
                room_key=_ROOM,
                session_id=_SESSION,
                outcome="replied",
                agent_duration_ms=12_000.0,
            ),
            # turn 2: user speaks at t=20, the agent call fails 32s later.
            TurnEnvelope(
                ts="1970-01-01T00:00:52Z",
                channel="line",
                room_key=_ROOM,
                session_id=_SESSION,
                outcome="agent_failed",
                agent_duration_ms=32_000.0,
                error="agent: ReadTimeout",
            ),
        ],
    )

    index = read_envelopes(conversations_dir, _ROOM)
    bindings = index.bind_messages([_user(1, 0.0), _user(2, 20.0)])

    assert bindings[1].outcome == "replied"
    assert bindings[2].outcome == "agent_failed"


def test_overlapping_turns_keep_their_own_envelopes(tmp_path: Path) -> None:
    """A slow turn and a fast turn in flight together must not swap envelopes.

    Two user messages 3s apart; turn 1 takes 120s, turn 2 takes 2s, so the
    envelopes land in the REVERSE order of the messages (t=5 then t=120). A
    per-message "first envelope after me" lookup hands the fast envelope to both
    messages; binding oldest envelope first hands each message its own.
    """
    conversations_dir = tmp_path / "_conversations"
    _write_envelopes(
        conversations_dir,
        [
            TurnEnvelope(
                ts="1970-01-01T00:00:05Z",
                channel="line",
                room_key=_ROOM,
                session_id=_SESSION,
                outcome="replied",
                agent_duration_ms=2_000.0,
            ),
            TurnEnvelope(
                ts="1970-01-01T00:02:00Z",
                channel="line",
                room_key=_ROOM,
                session_id=_SESSION,
                outcome="agent_failed",
                agent_duration_ms=120_000.0,
                error="agent: ReadTimeout",
            ),
        ],
    )

    index = read_envelopes(conversations_dir, _ROOM)
    bindings = index.bind_messages([_user(1, 0.0), _user(2, 3.0)])

    assert bindings[1].agent_duration_ms == 120_000.0
    assert bindings[2].agent_duration_ms == 2_000.0


def test_one_envelope_never_labels_more_than_one_message(tmp_path: Path) -> None:
    """The join is one-to-one: four messages, one envelope, one label."""
    conversations_dir = tmp_path / "_conversations"
    _write_envelopes(
        conversations_dir,
        [
            TurnEnvelope(
                ts="1970-01-01T00:00:30Z",
                channel="line",
                room_key=_ROOM,
                session_id=_SESSION,
                outcome="agent_failed",
                error="agent: ReadTimeout",
            )
        ],
    )

    index = read_envelopes(conversations_dir, _ROOM)
    bindings = index.bind_messages([_user(i, float(i)) for i in range(1, 5)])

    assert len(bindings) == 1
    # The LATEST message at or before the envelope's stamp, not the earliest.
    assert set(bindings) == {4}


def test_binding_never_looks_backwards(tmp_path: Path) -> None:
    """A message after the last envelope has no envelope — not the previous turn's."""
    conversations_dir = tmp_path / "_conversations"
    _write_envelopes(
        conversations_dir,
        [
            TurnEnvelope(
                ts="1970-01-01T00:00:12Z",
                channel="line",
                room_key=_ROOM,
                session_id=_SESSION,
                outcome="replied",
            )
        ],
    )

    index = read_envelopes(conversations_dir, _ROOM)

    assert index.bind_messages([_user(1, 13.0)]) == {}
    assert index.bound == set()


def test_unreadable_envelope_lines_are_counted_not_swallowed(tmp_path: Path) -> None:
    """A line that will not parse, and one from a newer schema, both surface."""
    conversations_dir = tmp_path / "_conversations"
    good = TurnEnvelope(ts="1970-01-01T00:00:12Z", channel="line", room_key=_ROOM, outcome="reset")
    ahead = json.loads(good.model_dump_json())
    ahead["schema_version"] = 99
    (conversations_dir / f"{_ROOM}.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (conversations_dir / f"{_ROOM}.jsonl").write_text(
        "\n".join(
            [
                good.model_dump_json(),
                "{not json at all",
                json.dumps({"channel": "line"}),  # valid JSON, invalid envelope
                json.dumps(ahead),
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    index = read_envelopes(conversations_dir, _ROOM)

    assert len(index.envelopes) == 2
    assert index.unreadable_lines == 2
    assert index.future_schema_lines == 1


def test_two_distinct_tool_results_at_one_timestamp_are_both_kept(tmp_path: Path) -> None:
    """Parallel tool calls return in the same tick — dedupe must not merge them."""
    path = tmp_path / "data" / _ROOM / "state.db"
    _build_state_db(path)
    writable = sqlite3.connect(path)
    with writable:
        for tool_call_id, content in (("call_a", '{"ok": 1}'), ("call_b", '{"ok": 2}')):
            writable.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, "
                "timestamp) VALUES (?,?,?,?,?,?)",
                (_SESSION, "tool", content, tool_call_id, "math", 2_000.0),
            )
        # Two user messages inside the same second are distinct too.
        for content in ("第一句", "第二句"):
            writable.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                (_SESSION, "user", content, 2_001.0),
            )
    writable.close()

    connection = connect_state_db(path)
    try:
        rows = read_messages(connection, since=2_000.0)
    finally:
        connection.close()

    assert [row.content for row in rows] == ['{"ok": 1}', '{"ok": 2}', "第一句", "第二句"]


def test_compaction_copies_are_still_collapsed(tmp_path: Path) -> None:
    """The row compaction re-inserted is byte-identical — it must still show once."""
    path = tmp_path / "data" / _ROOM / "state.db"
    _build_state_db(path)
    writable = sqlite3.connect(path)
    with writable:
        for compacted, active in ((1, 0), (1, 0), (0, 1)):
            writable.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, compacted, active) "
                "VALUES (?,?,?,?,?,?)",
                (_SESSION, "user", "壓縮前後都是同一句", 3_000.0, compacted, active),
            )
    writable.close()

    connection = connect_state_db(path)
    try:
        rows = read_messages(connection, since=3_000.0)
    finally:
        connection.close()

    assert [row.content for row in rows] == ["壓縮前後都是同一句"]


# ---------------------------------------------------------------------------
# CLI — a room that cannot be read must not abort the whole loop
# ---------------------------------------------------------------------------


def _add_broken_room(data_dir: Path, room_id: str = "line_broken") -> Path:
    """Put a file named state.db that is not a database into a second room dir."""
    path = data_dir / room_id / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not a sqlite database", encoding="utf-8")
    return path


def test_cli_rooms_skips_an_unreadable_room_and_keeps_going(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One corrupt state.db must not cost the operator every other room's line."""
    _add_broken_room(data_dir)

    out = _run(data_dir, ["rooms"], capsys)

    assert _ROOM in out
    assert "5000" in out


def test_cli_rooms_warns_about_the_room_it_skipped(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skipping is reported on stderr, never silently."""
    _add_broken_room(data_dir)

    assert conversations.main(["--data-dir", str(data_dir), "rooms"]) == 0
    result = capsys.readouterr()

    assert "line_broken" in result.err
    assert "unreadable state.db" in result.err
    assert _ROOM in result.out


def test_cli_search_skips_an_unreadable_room_and_keeps_going(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cross-room search still returns the good room's hits."""
    _add_broken_room(data_dir)

    assert conversations.main(["--data-dir", str(data_dir), "search", "有什麼"]) == 0
    result = capsys.readouterr()

    assert "我的行事曆上有什麼會議" in result.out
    assert "line_broken" in result.err


def test_cli_show_of_an_unreadable_room_exits_nonzero_without_a_traceback(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`show` on a corrupt db reports it and exits 1."""
    _add_broken_room(data_dir)

    assert conversations.main(["--data-dir", str(data_dir), "show", "line_broken"]) == 1
    assert "unreadable state.db" in capsys.readouterr().err


def test_search_falls_back_to_like_without_the_trigram_index(tmp_path: Path) -> None:
    """An older state.db with no messages_fts_trigram still searches, more slowly."""
    path = tmp_path / "data" / _ROOM / "state.db"
    _build_state_db(path)
    writable = sqlite3.connect(path)
    with writable:
        writable.execute("DROP TABLE messages_fts_trigram")
    writable.close()

    connection = connect_state_db(path)
    try:
        hits = search_messages(connection, "有什麼")
    finally:
        connection.close()

    assert [hit.role for hit in hits] == ["user"]


# ---------------------------------------------------------------------------
# CLI — argument errors and a room whose container never answered
# ---------------------------------------------------------------------------


def test_cli_rejects_a_malformed_since_with_exit_code_2(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd --since is a usage error, not a traceback."""
    with pytest.raises(SystemExit) as exit_info:
        conversations.main(["--data-dir", str(data_dir), "show", _ROOM, "--since", "yesterday"])

    assert exit_info.value.code == 2
    assert "invalid --since" in capsys.readouterr().err


def test_cli_stats_prints_zero_latency_percentiles(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 0.0 ms sample is a real measurement — it must not read as 'no samples'."""
    envelope = TurnEnvelope(
        ts="1970-01-01T00:16:46Z",
        channel="line",
        room_key=_ROOM,
        session_id=_SESSION,
        outcome="replied",
        agent_duration_ms=0.0,
    )
    (data_dir / "_conversations" / f"{_ROOM}.jsonl").write_text(
        envelope.model_dump_json() + "\n", encoding="utf-8"
    )

    out = _run(data_dir, ["stats"], capsys)

    assert "p50=0ms p95=0ms (n=1)" in out
    assert "no samples" not in out


def test_cli_show_renders_an_agent_failed_turn_that_never_reached_hermes(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A room whose container won't start has no state.db rows for those turns.

    Without rendering the envelope, the operator sees an empty transcript for
    exactly the room that is broken.
    """
    envelope = TurnEnvelope(
        ts="1970-01-01T02:00:00Z",
        channel="line",
        room_key=_ROOM,
        outcome="agent_failed",
        inbound_text="幫我查一下今天的信",
        error="container: 500 Server Error",
    )
    (data_dir / "_conversations" / f"{_ROOM}.jsonl").write_text(
        envelope.model_dump_json() + "\n", encoding="utf-8"
    )

    out = _run(data_dir, ["show", _ROOM], capsys)

    assert "幫我查一下今天的信" in out
    assert "container: 500 Server Error" in out


def test_cli_show_does_not_duplicate_an_agent_failed_turn_hermes_recorded(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fixture's agent_failed turn DID reach Hermes — still rendered exactly once."""
    out = _run(data_dir, ["show", _ROOM], capsys)

    assert out.count("幫我改到四點") == 1


# ---------------------------------------------------------------------------
# CLI — envelope-file warnings and retention (prune)
# ---------------------------------------------------------------------------


def test_cli_warns_about_envelope_lines_it_could_not_parse(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dropped line must be visible — a silent gap reads as 'that turn never happened'."""
    path = data_dir / "_conversations" / f"{_ROOM}.jsonl"
    good = TurnEnvelope(ts="1970-01-01T00:16:46Z", channel="line", room_key=_ROOM, outcome="reset")
    ahead = json.loads(good.model_dump_json())
    ahead["schema_version"] = 99
    path.write_text(
        "\n".join([good.model_dump_json(), "{broken", json.dumps(ahead)]) + "\n", encoding="utf-8"
    )

    assert conversations.main(["--data-dir", str(data_dir), "show", _ROOM]) == 0
    err = capsys.readouterr().err

    assert "1 envelope line(s)" in err
    assert "could not be parsed" in err
    assert "schema_version newer than" in err


def test_cli_prune_drops_only_the_lines_older_than_the_cutoff(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSONL is the only sink holding raw text, so prune is its retention lever."""
    path = data_dir / "_conversations" / f"{_ROOM}.jsonl"
    old = TurnEnvelope(ts="1970-01-01T00:00:01Z", channel="line", room_key=_ROOM, outcome="reset")
    recent = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    new = TurnEnvelope(ts=recent, channel="line", room_key=_ROOM, outcome="blocked")
    path.write_text(
        "\n".join([old.model_dump_json(), "{unreadable", new.model_dump_json()]) + "\n",
        encoding="utf-8",
    )

    out = _run(data_dir, ["prune", "--older-than", "1d"], capsys)

    kept = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:]]
    assert "dropped 1, kept 2" in out
    # The unreadable line survives: it is evidence, not an expired record.
    assert path.read_text(encoding="utf-8").splitlines()[0] == "{unreadable"
    assert [entry["outcome"] for entry in kept] == ["blocked"]


def test_cli_prune_dry_run_writes_nothing(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run reports the same counts and leaves the file (and no .tmp) alone."""
    path = data_dir / "_conversations" / f"{_ROOM}.jsonl"
    before = path.read_text(encoding="utf-8")

    out = _run(data_dir, ["prune", "--older-than", "1d", "--room", _ROOM, "--dry-run"], capsys)

    assert "would drop 3, kept 0" in out
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_name(f"{path.name}.tmp").exists()
