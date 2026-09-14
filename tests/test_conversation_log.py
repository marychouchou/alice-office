from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import structlog

from alice_office_router.config import Settings
from alice_office_router.conversation_log import (
    SCHEMA_VERSION,
    TURN_EVENT,
    Outcome,
    TurnEnvelope,
    record_turn,
)

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"

_ALL_OUTCOMES: tuple[Outcome, ...] = (
    "replied",
    "observed",
    "reset",
    "blocked",
    "agent_failed",
    "silence",
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Build a Settings instance pointed at a temp DATA_DIR.

    Args:
        tmp_path: pytest's per-test temp directory, used as DATA_DIR.
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance suitable for unit tests.
    """
    defaults: dict[str, object] = {
        "LINE_CHANNEL_SECRET": TEST_SECRET,
        "LINE_CHANNEL_ACCESS_TOKEN": TEST_TOKEN,
        "HERMES_API_SERVER_KEY": "test_api_server_key",
        "DATA_DIR": tmp_path,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _envelope(outcome: Outcome, **overrides: object) -> TurnEnvelope:
    """Build a TurnEnvelope for one outcome, with sensible defaults."""
    fields: dict[str, object] = {
        "channel": "line",
        "room_key": "line_room_AAA",
        "outcome": outcome,
    }
    fields.update(overrides)
    return TurnEnvelope(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JSONL sink — one line per turn, readable back as a TurnEnvelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", _ALL_OUTCOMES)
def test_each_outcome_round_trips_through_the_jsonl_file(tmp_path: Path, outcome: Outcome) -> None:
    """Every outcome writes exactly one line that parses back into the same envelope."""
    settings = _settings(tmp_path)
    envelope = _envelope(
        outcome,
        session_id="line_room_AAA#3",
        inbound_text=None if outcome == "replied" else "早安",
        rotated=True,
        agent_duration_ms=1234.5,
        prompt_tokens=27000,
        delivered=True,
    )

    record_turn(envelope, settings)

    path = settings.room_conversation_log("line_room_AAA")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = TurnEnvelope.model_validate_json(lines[0])
    assert parsed == envelope
    assert parsed.outcome == outcome
    assert parsed.schema_version == SCHEMA_VERSION


def test_turns_append_rather_than_overwrite(tmp_path: Path) -> None:
    """Successive turns for one room accumulate as separate lines in its file."""
    settings = _settings(tmp_path)

    record_turn(_envelope("observed", inbound_text="第一則"), settings)
    record_turn(_envelope("replied", session_id="line_room_AAA"), settings)

    lines = settings.room_conversation_log("line_room_AAA").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["outcome"] for line in lines] == ["observed", "replied"]


def test_each_room_gets_its_own_file(tmp_path: Path) -> None:
    """Two rooms never share a file — the room key names it (isolation boundary)."""
    settings = _settings(tmp_path)

    record_turn(_envelope("replied", room_key="line_room_AAA"), settings)
    record_turn(_envelope("replied", room_key="line_C1"), settings)

    written = sorted(path.name for path in settings.conversations_dir.iterdir())
    assert written == ["line_C1.jsonl", "line_room_AAA.jsonl"]


def test_file_lives_outside_the_rooms_own_data_dir(tmp_path: Path) -> None:
    """The envelope file must not land inside data/<room>/ (mounted into its container)."""
    settings = _settings(tmp_path)

    record_turn(_envelope("replied"), settings)

    assert settings.conversations_dir == tmp_path / "_conversations"
    assert not (tmp_path / "line_room_AAA").exists()


def test_envelope_file_and_directory_are_owner_only(tmp_path: Path) -> None:
    """This file holds the only copy of the text of every turn that never ran."""
    settings = _settings(tmp_path)

    record_turn(_envelope("blocked", inbound_text="沒授權的訊息"), settings)

    path = settings.room_conversation_log("line_room_AAA")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.conversations_dir.stat().st_mode) == 0o700


def test_unwritable_directory_is_logged_not_raised(tmp_path: Path) -> None:
    """A file-system failure must never propagate into the turn that produced it."""
    blocker = tmp_path / "_conversations"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = _settings(tmp_path)

    record_turn(_envelope("replied"), settings)  # must not raise


# ---------------------------------------------------------------------------
# Disable flag
# ---------------------------------------------------------------------------


def test_disabled_flag_writes_no_file(tmp_path: Path) -> None:
    """CONVERSATION_LOG_ENABLED=false leaves the filesystem untouched."""
    settings = _settings(tmp_path, CONVERSATION_LOG_ENABLED=False)

    record_turn(_envelope("replied"), settings)

    assert not settings.conversations_dir.exists()


def test_disabled_flag_still_emits_the_log_event(tmp_path: Path) -> None:
    """Disabling the file sink must not disable the collector (stdout) path."""
    settings = _settings(tmp_path, CONVERSATION_LOG_ENABLED=False)

    with structlog.testing.capture_logs() as captured:
        record_turn(_envelope("blocked", inbound_text="早安"), settings)

    assert [entry["event"] for entry in captured] == [TURN_EVENT]


# ---------------------------------------------------------------------------
# Log (collector) sink
# ---------------------------------------------------------------------------


def test_log_event_carries_the_envelope_fields(tmp_path: Path) -> None:
    """The stdout line is the envelope, field by field, under `conversation_turn`."""
    settings = _settings(tmp_path)
    envelope = _envelope(
        "agent_failed",
        session_id="line_room_AAA#2",
        inbound_text="幫我查",
        error="agent: boom",
        agent_duration_ms=42.0,
    )

    with structlog.testing.capture_logs() as captured:
        record_turn(envelope, settings)

    assert len(captured) == 1
    entry = captured[0]
    assert entry["event"] == TURN_EVENT
    assert entry["room_key"] == "line_room_AAA"
    assert entry["outcome"] == "agent_failed"
    assert entry["session_id"] == "line_room_AAA#2"
    assert entry["error"] == "agent: boom"
    assert entry["agent_duration_ms"] == 42.0


def test_log_event_never_carries_message_text_or_speaker_identity(tmp_path: Path) -> None:
    """The collector stream is metadata only — text and sender stay in the JSONL file."""
    settings = _settings(tmp_path)
    envelope = _envelope(
        "blocked",
        inbound_text="我的身分證字號是 A123456789",
        is_group=True,
        sender_id="U1234567890",
        sender_name="王小明",
        gate_status="blocked",
    )

    with structlog.testing.capture_logs() as captured:
        record_turn(envelope, settings)

    entry = captured[0]
    assert "inbound_text" not in entry
    assert "sender_id" not in entry
    assert "sender_name" not in entry
    assert "A123456789" not in json.dumps(entry, ensure_ascii=False)
    # Metadata still rides the stream, so the turn is still queryable in Loki.
    assert entry["outcome"] == "blocked"
    assert entry["gate_status"] == "blocked"
    assert entry["is_group"] is True
    # ...and the file the flag gates keeps the full record.
    written = json.loads(
        settings.room_conversation_log("line_room_AAA").read_text(encoding="utf-8").splitlines()[0]
    )
    assert written["inbound_text"] == "我的身分證字號是 A123456789"
    assert written["sender_id"] == "U1234567890"
    assert written["sender_name"] == "王小明"


def test_disabled_flag_keeps_text_out_of_both_sinks(tmp_path: Path) -> None:
    """With the file sink off, nothing anywhere retains the message text."""
    settings = _settings(tmp_path, CONVERSATION_LOG_ENABLED=False)

    with structlog.testing.capture_logs() as captured:
        record_turn(_envelope("blocked", inbound_text="秘密", sender_id="U1"), settings)

    assert "秘密" not in json.dumps(captured[0], ensure_ascii=False)
    assert not settings.conversations_dir.exists()


# ---------------------------------------------------------------------------
# Model rules
# ---------------------------------------------------------------------------


def test_defaults_are_the_documented_ones() -> None:
    """A bare envelope carries the schema version, a ts, and no delivery verdict."""
    envelope = _envelope("observed")

    assert envelope.schema_version == SCHEMA_VERSION
    assert envelope.ts.endswith("Z")
    assert envelope.delivered is None
    assert envelope.inbound_text is None


def test_unknown_outcome_is_rejected() -> None:
    """The outcome vocabulary is closed — a typo must fail loudly, not be stored."""
    with pytest.raises(ValueError):
        TurnEnvelope(channel="line", room_key="line_room_AAA", outcome="delivered")  # type: ignore[arg-type]
