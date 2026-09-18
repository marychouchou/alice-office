from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pydantic import ValidationError

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings
from alice_office_router.core import AGENT_FAILURE_NOTICE, AGENT_TIMEOUT_NOTICE
from alice_office_router.hermes_client import AgentReply
from alice_office_router.session_hygiene import RESET_CONFIRMATION, SessionState, load_state

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"

_NOTICE_MSG = "缺少 Drive 授權：https://example.com/oauth/start?user_id=room_aaa"


def _settings(**overrides: object) -> Settings:
    """Build a Settings instance with test credentials, allowing overrides.

    Args:
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance suitable for unit tests.
    """
    defaults: dict[str, object] = {
        "LINE_CHANNEL_SECRET": TEST_SECRET,
        "LINE_CHANNEL_ACCESS_TOKEN": TEST_TOKEN,
        "HERMES_API_SERVER_KEY": "test_api_server_key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _seed_session(settings: Settings, room_key: str, state: SessionState) -> None:
    """Write a room's session.json directly, seeding a starting epoch/watermark."""
    path = settings.room_router_state_dir(room_key) / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(), encoding="utf-8")


def _recording_ask(
    responder: Callable[[str], AgentReply | Exception],
) -> tuple[Callable[..., object], list[SimpleNamespace]]:
    """Build an async ask_hermes_agent stand-in that records its calls.

    Args:
        responder: Maps a session_id to the AgentReply to return, or an
            Exception instance to raise for that session id.

    Returns:
        (fake_ask, calls) — patch core.ask_hermes_agent with fake_ask and read
        the recorded (session_id, text, system) tuples from calls afterwards.
    """
    calls: list[SimpleNamespace] = []

    async def _ask(
        base_url: str,
        session_id: str,
        text: str,
        api_key: str,
        *,
        idle_timeout_seconds: float,
        max_seconds: float,
        system: str | None = None,
    ) -> AgentReply:
        calls.append(SimpleNamespace(session_id=session_id, text=text, system=system))
        outcome = responder(session_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _ask, calls


def _msg(text: str = "哈囉", room_key: str = "line_room_AAA") -> InboundMessage:
    """Build a channel-free InboundMessage for the tests.

    Args:
        text: The inbound plain text.
        room_key: The room key core routes on.

    Returns:
        An InboundMessage tagged with the "line" channel.
    """
    return InboundMessage(channel="line", room_key=room_key, text=text)


def _group_msg(
    text: str = "幫我排會議",
    *,
    addressed: bool = True,
    sender_id: str | None = "U1",
    sender_name: str | None = "王小明",
) -> InboundMessage:
    """Build a group InboundMessage (is_group=True) for the group-path tests.

    Args:
        text: The inbound plain text.
        addressed: Whether the message is directed at the bot.
        sender_id: The group speaker's native id.
        sender_name: The group speaker's resolved display name.

    Returns:
        A group InboundMessage tagged with the "line" channel.
    """
    return InboundMessage(
        channel="line",
        room_key="line_C1",
        text=text,
        is_group=True,
        addressed=addressed,
        sender_id=sender_id,
        sender_name=sender_name,
    )


@pytest.fixture
def warmups() -> Iterator[dict[str, asyncio.Task[None]]]:
    """Give each warm-up test a clean warm-up registry.

    Yields:
        core's `_warmups` dict, emptied before and after the test so an
        in-flight task from another test can never change the outcome.
    """
    from alice_office_router.core import _warmups

    _warmups.clear()
    yield _warmups
    _warmups.clear()


@pytest.fixture
def probed() -> Iterator[set[str]]:
    """Give each warm-up test a clean agent-probe registry.

    Yields:
        core's `_probed` set, emptied before and after the test so a room
        another test already probed never skips this test's probe.
    """
    from alice_office_router.core import _probed

    _probed.clear()
    yield _probed
    _probed.clear()


async def _settle_warmups() -> None:
    """Await every in-flight container warm-up.

    Must be called *inside* the test's `patch(...)` block: the warm-up task
    resolves `core.get_or_create_container` only once it first runs, which is
    after the call that started it has returned, so a test that leaves the
    patch before settling would hand the real docker call to a worker thread.
    """
    from alice_office_router.core import _warmups

    await asyncio.gather(*_warmups.values())


# ---------------------------------------------------------------------------
# process_inbound — normal flow
# ---------------------------------------------------------------------------


async def test_ok_status_returns_only_agent_reply(tmp_path: Path) -> None:
    """An "ok" gate result resolves the container, asks the agent, and returns its reply."""
    from alice_office_router.core import process_inbound
    from alice_office_router.group_context import DIRECT_SYSTEM_PROMPT

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="哈囉，我是 Hermes")),
        ) as mock_ask,
    ):
        texts = (await process_inbound(_msg(), settings)).texts

    mock_get_container.assert_called_once_with("line_room_AAA", settings)
    # Epoch 0 (fresh room, no rotation) sends the bare room_key and no handoff.
    # Every 1:1 turn carries DIRECT_SYSTEM_PROMPT, the deployment-level reply-
    # shape instruction (LINE-sized answers, big jobs delivered in chunks).
    mock_ask.assert_awaited_once_with(
        "http://hermes_line_room_AAA:8642",
        "line_room_AAA",
        "哈囉",
        "test_api_server_key",
        idle_timeout_seconds=120.0,
        max_seconds=3600.0,
        system=DIRECT_SYSTEM_PROMPT,
    )
    assert texts == ["哈囉，我是 Hermes"]
    # Epoch 0 stays 0 with no trigger — backward compatible with existing sessions.
    assert load_state(settings, "line_room_AAA").epoch == 0


async def test_outbox_marker_in_a_reply_becomes_a_download_url(tmp_path: Path) -> None:
    """A reply carrying outbox://<token> leaves core as a real, user-clickable URL."""
    from alice_office_router.core import process_inbound

    room_key = "line_room_AAA"
    token = "Qm3fZ9xL-aB7cD1eF4gH6iJ8kL0mN2oP5qR7sT9uV1w"
    outbox = tmp_path / room_key / "outbox" / token
    outbox.mkdir(parents=True)
    (outbox / "report.pdf").write_bytes(b"%PDF-1.4 hi")
    settings = _settings(DATA_DIR=tmp_path, PUBLIC_BASE_URL="https://router.example.com")

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text=f"做好了：\noutbox://{token}")),
        ),
    ):
        texts = (await process_inbound(_msg(), settings)).texts

    assert texts == [f"做好了：\nhttps://router.example.com/files/{room_key}/{token}"]
    # The bytes now live outside the room's own mount, and the agent's copy is gone.
    assert (tmp_path / "_files" / room_key / token / "report.pdf").read_bytes() == b"%PDF-1.4 hi"
    assert not outbox.exists()


# ---------------------------------------------------------------------------
# process_inbound — Google OAuth gate
# ---------------------------------------------------------------------------


async def test_token_symlink_is_swapped_to_the_speaker_before_the_gate(tmp_path: Path) -> None:
    """Every turn repoints the room's tokens.json at the speaker, before anything reads it.

    Both the gate and the agent's Google MCPs read whatever tokens.json
    points at, so the swap has to be the first thing the turn does — and it
    has to name the speaker, not the room, in a group.
    """
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)
    order: list[str] = []

    with (
        patch("alice_office_router.core.select_member_tokens") as mock_select,
        patch("alice_office_router.core.check_google_authorization") as mock_gate,
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_C1:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="好")),
        ),
    ):
        mock_select.side_effect = lambda *args: order.append("select")
        mock_gate.side_effect = lambda *args: (order.append("gate"), ("ok", None))[1]
        await process_inbound(_group_msg(sender_id="U_SPEAKER"), settings)

    mock_select.assert_called_once_with(settings, "line_C1", "u_speaker")
    assert order == ["select", "gate"]


async def test_token_symlink_swap_for_a_direct_room_uses_the_room_key(tmp_path: Path) -> None:
    """A 1:1 room's member is the room itself, so it swaps to the same file every turn."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.select_member_tokens") as mock_select,
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="好")),
        ),
    ):
        await process_inbound(_msg(), settings)

    mock_select.assert_called_once_with(settings, "line_room_AAA", "line_room_aaa")


async def test_a_speaker_without_a_google_token_still_reaches_the_agent(
    tmp_path: Path, warmups: dict[str, asyncio.Task[None]]
) -> None:
    """The gate stopped blocking on 2026-09-18: an unauthorized room gets a real answer.

    Deliberately runs the real `check_google_authorization` (Google fully
    configured, no token anywhere) rather than a patched one — the point of
    the test is that this combination no longer short-circuits the turn.
    """
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path, PUBLIC_BASE_URL="https://router.example.com")
    settings.google_web_creds_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_web_creds_path.write_text("{}", encoding="utf-8")

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="今天是 9 月 18 日")),
        ) as mock_ask,
    ):
        result = await process_inbound(_msg("今天幾號"), settings)

    mock_get_container.assert_called_once_with("line_room_AAA", settings)
    mock_ask.assert_awaited_once()
    assert result.texts == ["今天是 9 月 18 日"]
    assert result.envelope.outcome == "replied"
    assert result.envelope.gate_status == "ok"
    # No warm-up is triggered any more: the turn itself resolved the container.
    assert warmups == {}


async def test_an_unidentified_group_speaker_still_reaches_the_agent(tmp_path: Path) -> None:
    """A group speaker LINE won't name has no token by definition — and is not gated for it."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path, PUBLIC_BASE_URL="https://router.example.com")
    settings.google_web_creds_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_web_creds_path.write_text("{}", encoding="utf-8")

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_C1:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="好的")),
        ) as mock_ask,
    ):
        result = await process_inbound(_group_msg(sender_id=None, sender_name=None), settings)

    mock_ask.assert_awaited_once()
    assert result.texts == ["好的"]
    assert result.envelope.gate_status == "ok"


# ---------------------------------------------------------------------------
# warm_room — container + agent warm-up (no production caller until step 5 of
# docs/google-auth-per-member-plan.md wires LINE follow/join to it)
# ---------------------------------------------------------------------------


async def test_warm_room_starts_the_container_then_probes_the_agent(
    warmups: dict[str, asyncio.Task[None]], probed: set[str]
) -> None:
    """The warm-up runs in the background and spends one throwaway turn on the agent."""
    from alice_office_router.core import WARMUP_PROMPT, warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()) as mock_ask,
        patch("alice_office_router.core.delete_hermes_session", new=AsyncMock()) as mock_delete,
    ):
        warm_room("line_room_AAA", settings)
        # Returns immediately: the caller never waits on the warm-up.
        assert "line_room_AAA" in warmups
        await _settle_warmups()

    mock_get_container.assert_called_once_with("line_room_AAA", settings)
    # The one agent call is the throwaway probe, on its own session id and its
    # own (short) ceiling — no user text is ever sent here.
    mock_ask.assert_awaited_once_with(
        "http://hermes_line_room_AAA:8642",
        "warmup-probe",
        WARMUP_PROMPT,
        "test_api_server_key",
        idle_timeout_seconds=120.0,
        max_seconds=120.0,
    )
    # ...and the probe leaves nothing behind in the room's state.db.
    mock_delete.assert_awaited_once_with(
        "http://hermes_line_room_AAA:8642", "warmup-probe", "test_api_server_key"
    )
    assert "line_room_AAA" in probed
    assert warmups == {}


async def test_warm_room_probe_failure_is_a_warning_and_leaves_the_room_unprobed(
    warmups: dict[str, asyncio.Task[None]], probed: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A failed probe costs the room's first real turn its cold start, nothing else."""
    from alice_office_router.core import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("Hermes agent failed: x")),
        ),
        patch("alice_office_router.core.delete_hermes_session", new=AsyncMock()) as mock_delete,
        caplog.at_level(logging.WARNING, logger="alice_office_router.core"),
    ):
        warm_room("line_room_AAA", settings)
        await _settle_warmups()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("warm-up probe failed" in message for message in warnings)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # A half-run probe may still have made Hermes create the session.
    mock_delete.assert_awaited_once()
    # Not probed, so a later warm-up retries.
    assert probed == set()
    assert warmups == {}


async def test_warm_room_probe_session_delete_failure_is_a_warning_only(
    warmups: dict[str, asyncio.Task[None]], probed: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A stray probe session is worth a log line, not a re-probe of a warm agent."""
    from alice_office_router.core import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()),
        patch(
            "alice_office_router.core.delete_hermes_session",
            new=AsyncMock(side_effect=httpx.ConnectError("x")),
        ),
        caplog.at_level(logging.WARNING, logger="alice_office_router.core"),
    ):
        warm_room("line_room_AAA", settings)
        await _settle_warmups()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Could not delete warm-up session" in message for message in warnings)
    # The agent itself is warm — that is what `_probed` records.
    assert "line_room_AAA" in probed
    assert warmups == {}


async def test_warm_room_twice_probes_the_agent_only_once(
    warmups: dict[str, asyncio.Task[None]], probed: set[str]
) -> None:
    """A second warm-up after the first finished re-warms the container only."""
    from alice_office_router.core import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()) as mock_ask,
        patch("alice_office_router.core.delete_hermes_session", new=AsyncMock()),
    ):
        warm_room("line_room_AAA", settings)
        await _settle_warmups()
        warm_room("line_room_AAA", settings)
        await _settle_warmups()

    # Resolving an existing container is cheap; a second ~28k-token probe of an
    # already-warm agent is not. (Deduplication is in-flight only, so the
    # second call does re-run the container step.)
    assert mock_get_container.call_count == 2
    mock_ask.assert_awaited_once()
    assert probed == {"line_room_AAA"}


async def test_warm_room_probe_unexpected_error_ends_in_the_error_log(
    warmups: dict[str, asyncio.Task[None]], probed: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    """An exception the probe does not expect is logged, not left on a dead task."""
    from alice_office_router.core import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("alice_office_router.core.delete_hermes_session", new=AsyncMock()) as mock_delete,
        caplog.at_level(logging.ERROR, logger="alice_office_router.core"),
    ):
        warm_room("line_room_AAA", settings)
        await _settle_warmups()

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "warm-up failed" in errors[0]
    assert "agent: RuntimeError: boom" in errors[0]
    # Cleanup still ran on the way out.
    mock_delete.assert_awaited_once()
    assert probed == set()
    assert warmups == {}


async def test_warm_room_container_failure_is_logged_and_skips_the_probe(
    warmups: dict[str, asyncio.Task[None]], probed: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A warm-up that fails is logged for the operator and leaves no entry behind."""
    from alice_office_router.core import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            side_effect=RuntimeError("did not become ready"),
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()) as mock_ask,
        patch("alice_office_router.core.delete_hermes_session", new=AsyncMock()),
        caplog.at_level(logging.ERROR),
    ):
        warm_room("line_room_AAA", settings)
        await _settle_warmups()

    # No container, no probe: the second step never gets a URL to talk to.
    mock_ask.assert_not_awaited()
    assert probed == set()
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "warm-up failed" in errors[0]
    assert "container: RuntimeError: did not become ready" in errors[0]
    # A failed warm-up leaves no entry behind, so the next trigger retries.
    assert warmups == {}


async def test_warm_room_twice_warms_once_while_in_flight(
    warmups: dict[str, asyncio.Task[None]], probed: set[str]
) -> None:
    """A second trigger during a running warm-up reuses it, not a second thread."""
    from alice_office_router.core import warm_room

    settings = _settings()
    release = threading.Event()

    def _slow_container(room_key: str, config: Settings) -> str:
        release.wait(2)
        return "http://hermes_line_room_AAA:8642"

    with (
        patch(
            "alice_office_router.core.get_or_create_container", side_effect=_slow_container
        ) as mock_get_container,
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()),
        patch("alice_office_router.core.delete_hermes_session", new=AsyncMock()),
    ):
        try:
            warm_room("line_room_AAA", settings)
            # Let the first task reach its to_thread await before the second
            # trigger, so the dedup is exercised on a genuinely in-flight one.
            await asyncio.sleep(0)
            warm_room("line_room_AAA", settings)
            assert len(warmups) == 1
        finally:
            # Release the parked thread and settle inside the patch even when
            # the assertion fails, so no task outlives the mock.
            release.set()
            await _settle_warmups()

    assert mock_get_container.call_count == 1


async def test_cancel_warmups_logs_and_cancels_in_flight_tasks(
    warmups: dict[str, asyncio.Task[None]], probed: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Shutdown cancels the tracked warm-ups and says so; the thread finishes on its own."""
    from alice_office_router.core import cancel_warmups, warm_room

    settings = _settings()
    release = threading.Event()

    def _slow_container(room_key: str, config: Settings) -> str:
        release.wait(2)
        return "http://hermes_line_room_AAA:8642"

    with (
        patch("alice_office_router.core.get_or_create_container", side_effect=_slow_container),
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()),
        patch("alice_office_router.core.delete_hermes_session", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="alice_office_router.core"),
    ):
        try:
            warm_room("line_room_AAA", settings)
            task = warmups["line_room_AAA"]
            # A task cancelled before its first step never enters the
            # coroutine (so nothing logs); let it reach the to_thread await.
            await asyncio.sleep(0)
            cancel_warmups()
            await asyncio.wait([task])
        finally:
            release.set()

    assert task.cancelled()
    assert any("cancelled at shutdown" in record.getMessage() for record in caplog.records)
    assert warmups == {}


async def test_warm_room_retries_after_the_previous_one_finished(
    warmups: dict[str, asyncio.Task[None]],
) -> None:
    """Deduplication is in-flight only: once a warm-up has finished, the next one warms again."""
    from alice_office_router.core import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()),
    ):
        warm_room("line_room_AAA", settings)
        await _settle_warmups()
        warm_room("line_room_AAA", settings)
        await _settle_warmups()

    assert mock_get_container.call_count == 2


async def test_notice_returns_notice_then_agent_reply(tmp_path: Path) -> None:
    """A "notice" gate result returns the notice followed by the agent reply."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch(
            "alice_office_router.core.check_google_authorization",
            return_value=("notice", _NOTICE_MSG),
        ),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="哈囉，我是 Hermes")),
        ),
    ):
        texts = (await process_inbound(_msg(), settings)).texts

    mock_get_container.assert_called_once_with("line_room_AAA", settings)
    assert len(texts) == 2
    assert "oauth/start" in texts[0]
    assert texts[1] == "哈囉，我是 Hermes"


# ---------------------------------------------------------------------------
# process_inbound — downstream failure handling
# ---------------------------------------------------------------------------


async def test_agent_error_returns_the_failure_notice(tmp_path: Path) -> None:
    """A Hermes agent failure is swallowed, but the room still gets a fixed notice."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
    ):
        texts = (await process_inbound(_msg(), settings)).texts

    assert texts == [AGENT_FAILURE_NOTICE]


async def test_container_error_returns_the_failure_notice_and_skips_agent() -> None:
    """A container failure is swallowed, the agent is never asked, the room is told."""
    from alice_office_router.core import process_inbound

    settings = _settings()

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            side_effect=RuntimeError("boom"),
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()) as mock_ask,
    ):
        texts = (await process_inbound(_msg(), settings)).texts

    mock_ask.assert_not_awaited()
    assert texts == [AGENT_FAILURE_NOTICE]


async def test_notice_kept_when_agent_fails(tmp_path: Path) -> None:
    """When the agent fails after a notice, the notice still rides ahead of the failure text."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch(
            "alice_office_router.core.check_google_authorization",
            return_value=("notice", _NOTICE_MSG),
        ),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
    ):
        texts = (await process_inbound(_msg(), settings)).texts

    assert len(texts) == 2
    assert "Drive" in texts[0]
    assert texts[1] == AGENT_FAILURE_NOTICE


# ---------------------------------------------------------------------------
# process_inbound — group path
# ---------------------------------------------------------------------------


async def test_unaddressed_group_message_is_observed_and_short_circuits() -> None:
    """An unaddressed group message records observed context and skips gate + agent."""
    from alice_office_router.core import process_inbound

    settings = _settings()

    with (
        patch("alice_office_router.core.record_observed") as mock_record,
        patch("alice_office_router.core.check_google_authorization") as mock_gate,
        patch("alice_office_router.core.get_or_create_container") as mock_container,
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()) as mock_ask,
    ):
        texts = (
            await process_inbound(_group_msg("userA 跟 userB 問早", addressed=False), settings)
        ).texts

    assert texts == []
    mock_record.assert_called_once_with(settings, "line_C1", "U1", "王小明", "userA 跟 userB 問早")
    # Before the OAuth gate: the observe short-circuit never touches it.
    mock_gate.assert_not_called()
    mock_container.assert_not_called()
    mock_ask.assert_not_awaited()


async def test_addressed_group_builds_tagged_prompt_under_system_message(tmp_path: Path) -> None:
    """An addressed group message asks the agent with the tagged prompt + group system message."""
    from alice_office_router.core import process_inbound
    from alice_office_router.group_context import GROUP_SYSTEM_PROMPT, ObservedMessage

    settings = _settings(DATA_DIR=tmp_path)
    observed = [ObservedMessage(ts=1.0, sender_id="U2", sender_name="李小華", text="早")]

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch("alice_office_router.core.peek_observed", return_value=observed),
        patch("alice_office_router.core.clear_observed") as mock_clear,
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_C1:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="好的，已安排")),
        ) as mock_ask,
    ):
        texts = (await process_inbound(_group_msg(), settings)).texts

    assert texts == ["好的，已安排"]
    # Clears exactly the peeked records, not the whole file, so anything
    # observed during the agent call survives (see clear_observed).
    mock_clear.assert_called_once_with(settings, "line_C1", observed)
    prompt = mock_ask.call_args.args[2]
    assert "[李小華|U2] 早" in prompt
    assert "[王小明|U1] 幫我排會議" in prompt
    assert "[背景結束]" in prompt
    assert mock_ask.call_args.kwargs["system"] == GROUP_SYSTEM_PROMPT


async def test_group_agent_failure_keeps_buffer(tmp_path: Path) -> None:
    """A failed group call delivers the notice yet keeps the observed buffer for a retry."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch("alice_office_router.core.peek_observed", return_value=[]),
        patch("alice_office_router.core.clear_observed") as mock_clear,
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_C1:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
    ):
        texts = (await process_inbound(_group_msg(), settings)).texts

    assert texts == [AGENT_FAILURE_NOTICE]
    mock_clear.assert_not_called()


async def test_group_agent_failure_notifies_without_dropping_real_observed_background(
    tmp_path: Path,
) -> None:
    """Against the real buffer: the room is told, and the background survives untouched."""
    from alice_office_router.core import process_inbound
    from alice_office_router.group_context import peek_observed, record_observed

    settings = _settings(DATA_DIR=tmp_path)
    record_observed(settings, "line_C1", "U2", "李小華", "早安")

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_C1:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
    ):
        result = await process_inbound(_group_msg(), settings)

    assert result.texts == [AGENT_FAILURE_NOTICE]
    assert result.envelope.outcome == "agent_failed"
    # The notice must not be mistaken for an answer: nothing was folded in, so
    # the background is still owed to the next successful turn.
    observed = peek_observed(settings, "line_C1")
    assert [record.text for record in observed] == ["早安"]


async def test_group_silence_token_is_dropped_but_buffer_cleared(tmp_path: Path) -> None:
    """A silence-token reply is never delivered, yet the buffer is cleared (agent answered)."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch("alice_office_router.core.peek_observed", return_value=[]),
        patch("alice_office_router.core.clear_observed") as mock_clear,
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_C1:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="NO_REPLY")),
        ),
    ):
        texts = (await process_inbound(_group_msg(), settings)).texts

    assert texts == []
    mock_clear.assert_called_once_with(settings, "line_C1", [])


# ---------------------------------------------------------------------------
# process_inbound — manual reset command
# ---------------------------------------------------------------------------


async def test_reset_command_confirms_rotates_and_skips_agent(tmp_path: Path) -> None:
    """A "/new" rotates the epoch and returns only the confirmation — no gate/agent."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)
    _seed_session(settings, "line_room_AAA", SessionState(epoch=1))

    with (
        patch("alice_office_router.core.check_google_authorization") as mock_gate,
        patch("alice_office_router.core.get_or_create_container") as mock_container,
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()) as mock_ask,
    ):
        texts = (await process_inbound(_msg("/new"), settings)).texts

    assert texts == [RESET_CONFIRMATION]
    mock_gate.assert_not_called()
    mock_container.assert_not_called()
    mock_ask.assert_not_awaited()
    assert load_state(settings, "line_room_AAA").epoch == 2


async def test_group_reset_clears_observed_buffer(tmp_path: Path) -> None:
    """A group reset command also drops the observed background buffer."""
    from alice_office_router.core import process_inbound
    from alice_office_router.group_context import peek_observed, record_observed

    settings = _settings(DATA_DIR=tmp_path)
    record_observed(settings, "line_C1", "U2", "李小華", "早安")

    with (
        patch("alice_office_router.core.check_google_authorization") as mock_gate,
        patch("alice_office_router.core.get_or_create_container") as mock_container,
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()) as mock_ask,
    ):
        texts = (await process_inbound(_group_msg("/new"), settings)).texts

    assert texts == [RESET_CONFIRMATION]
    assert peek_observed(settings, "line_C1") == []
    mock_gate.assert_not_called()
    mock_container.assert_not_called()
    mock_ask.assert_not_awaited()


# ---------------------------------------------------------------------------
# process_inbound — automatic rotation (idle / handoff)
# ---------------------------------------------------------------------------


async def test_idle_rotation_generates_handoff_then_injects_into_new_epoch(
    tmp_path: Path,
) -> None:
    """An idle room asks the OLD session for a handoff, then injects it into the NEW one."""
    from alice_office_router.core import process_inbound
    from alice_office_router.session_hygiene import HANDOFF_PROMPT

    settings = _settings(DATA_DIR=tmp_path, SESSION_IDLE_RESET_MINUTES=1440)
    _seed_session(
        settings,
        "line_room_AAA",
        SessionState(epoch=1, last_activity_ts=time.time() - 3 * 24 * 60 * 60),
    )
    replies = {
        "line_room_AAA#1": AgentReply(text="交接：未完成的是報價單"),
        "line_room_AAA#2": AgentReply(text="新回覆"),
    }
    fake_ask, calls = _recording_ask(lambda session_id: replies[session_id])

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=fake_ask),
    ):
        texts = (await process_inbound(_msg("今天的進度"), settings)).texts

    assert texts == ["新回覆"]
    # The retired session (#1) received the handoff prompt verbatim (the epoch
    # was already bumped by begin_turn before the summary request went out).
    handoff_call = next(c for c in calls if c.session_id == "line_room_AAA#1")
    assert handoff_call.text == HANDOFF_PROMPT
    # New session (#2) got the delimited handoff block prepended to the user text.
    new_call = next(c for c in calls if c.session_id == "line_room_AAA#2")
    assert "交接：未完成的是報價單" in new_call.text
    assert "今天的進度" in new_call.text
    assert new_call.text.startswith("[以下是上一段對話的交接摘要")
    assert load_state(settings, "line_room_AAA").epoch == 2


async def test_handoff_failure_still_rotates_clean_slate(tmp_path: Path) -> None:
    """A failed handoff summary logs and rotates anyway with plain (un-prefixed) text."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path, SESSION_IDLE_RESET_MINUTES=1440)
    _seed_session(
        settings,
        "line_room_AAA",
        SessionState(epoch=1, last_activity_ts=time.time() - 3 * 24 * 60 * 60),
    )

    def responder(session_id: str) -> AgentReply | Exception:
        if session_id == "line_room_AAA#1":
            return ValueError("summary boom")
        return AgentReply(text="新回覆")

    fake_ask, calls = _recording_ask(responder)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=fake_ask),
    ):
        texts = (await process_inbound(_msg("今天的進度"), settings)).texts

    assert texts == ["新回覆"]
    assert load_state(settings, "line_room_AAA").epoch == 2
    # No summary => the new epoch's first message is the plain user text.
    new_call = next(c for c in calls if c.session_id == "line_room_AAA#2")
    assert new_call.text == "今天的進度"
    assert "交接摘要" not in new_call.text


async def test_group_idle_rotation_wraps_tagged_prompt(tmp_path: Path) -> None:
    """The group path shares rotation: the handoff wraps the group-tagged prompt."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path, SESSION_IDLE_RESET_MINUTES=1440)
    _seed_session(
        settings, "line_C1", SessionState(epoch=1, last_activity_ts=time.time() - 3 * 24 * 60 * 60)
    )
    replies = {
        "line_C1#1": AgentReply(text="交接摘要內容"),
        "line_C1#2": AgentReply(text="好的，已安排"),
    }
    fake_ask, calls = _recording_ask(lambda session_id: replies[session_id])

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch("alice_office_router.core.peek_observed", return_value=[]),
        patch("alice_office_router.core.clear_observed"),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_C1:8642",
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=fake_ask),
    ):
        texts = (await process_inbound(_group_msg(), settings)).texts

    assert texts == ["好的，已安排"]
    new_call = next(c for c in calls if c.session_id == "line_C1#2")
    # Handoff block on top, then the group-tagged trigger line inside it.
    assert new_call.text.startswith("[以下是上一段對話的交接摘要")
    assert "[王小明|U1] 幫我排會議" in new_call.text
    assert new_call.system is not None  # still under the group system message


async def test_successful_turn_records_token_watermark(tmp_path: Path) -> None:
    """A successful turn writes back the reported prompt_tokens for the next idle/token check."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="回覆", prompt_tokens=4321)),
        ),
    ):
        await process_inbound(_msg(), settings)

    state = load_state(settings, "line_room_AAA")
    assert state.last_prompt_tokens == 4321
    assert state.epoch == 0


# ---------------------------------------------------------------------------
# process_inbound — the turn envelope (docs/logging-design.md §5.7)
# ---------------------------------------------------------------------------


async def test_envelope_outcome_replied_carries_session_and_latency(tmp_path: Path) -> None:
    """A normal turn records the exact session id sent, the latency and the tokens."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)
    _seed_session(settings, "line_room_AAA", SessionState(epoch=3))

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="回覆", prompt_tokens=27000)),
        ),
    ):
        result = await process_inbound(_msg(), settings)

    envelope = result.envelope
    assert envelope.outcome == "replied"
    assert envelope.session_id == "line_room_AAA#3"
    assert envelope.gate_status == "ok"
    assert envelope.prompt_tokens == 27000
    assert envelope.rotated is False
    assert envelope.agent_duration_ms is not None
    # A replied turn's text is already in state.db; never duplicated here.
    assert envelope.inbound_text is None
    # delivered belongs to the adapter, which has not run yet.
    assert envelope.delivered is None
    assert envelope.channel == "line"
    assert envelope.room_key == "line_room_AAA"


async def test_envelope_carries_the_reply_call_counts(tmp_path: Path) -> None:
    """tool_calls/api_calls flow AgentReply -> AgentTurn -> RouteResult -> envelope untouched."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(
                return_value=AgentReply(text="回覆", tool_calls=11, api_calls=14),
            ),
        ),
    ):
        result = await process_inbound(_msg(), settings)

    assert result.envelope.tool_calls == 11
    assert result.envelope.api_calls == 14


async def test_envelope_outcome_observed_keeps_the_text(tmp_path: Path) -> None:
    """An unaddressed group message exists nowhere else, so the envelope keeps its text."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    result = await process_inbound(_group_msg("大家早", addressed=False), settings)

    envelope = result.envelope
    assert envelope.outcome == "observed"
    assert envelope.inbound_text == "大家早"
    assert envelope.is_group is True
    assert envelope.addressed is False
    assert envelope.sender_id == "U1"
    assert envelope.sender_name == "王小明"
    assert envelope.session_id is None
    assert envelope.gate_status is None


async def test_envelope_outcome_reset(tmp_path: Path) -> None:
    """A manual reset never reaches the agent, so it only exists in the envelope."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with patch("alice_office_router.core.check_google_authorization") as mock_gate:
        result = await process_inbound(_msg("/new"), settings)

    mock_gate.assert_not_called()
    assert result.texts == [RESET_CONFIRMATION]
    assert result.envelope.outcome == "reset"
    assert result.envelope.inbound_text == "/new"
    assert result.envelope.gate_status is None


async def test_envelope_records_the_gate_status_of_a_normal_turn(tmp_path: Path) -> None:
    """The gate's verdict rides every envelope, so a room's turns stay queryable by it."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="好")),
        ),
    ):
        result = await process_inbound(_msg("幫我看 Drive"), settings)

    assert result.envelope.outcome == "replied"
    assert result.envelope.gate_status == "ok"
    # A replied turn's text is already in state.db; the envelope must not copy it.
    assert result.envelope.inbound_text is None
    assert result.envelope.session_id == "line_room_AAA"


async def test_envelope_outcome_agent_failed_records_the_error(tmp_path: Path) -> None:
    """A failed agent call records the session it failed under and why."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
    ):
        result = await process_inbound(_msg(), settings)

    envelope = result.envelope
    assert envelope.outcome == "agent_failed"
    assert envelope.error is not None and "boom" in envelope.error
    assert envelope.session_id == "line_room_AAA"
    assert envelope.agent_duration_ms is not None
    # A non-timeout failure is generic to the room; the detail stays in `error`.
    assert result.texts == [AGENT_FAILURE_NOTICE]


async def test_envelope_outcome_agent_failed_when_the_container_is_unreachable() -> None:
    """A container failure never gets a session id — the call never went out."""
    from alice_office_router.core import process_inbound

    settings = _settings()

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            side_effect=RuntimeError("no docker"),
        ),
    ):
        result = await process_inbound(_msg(), settings)

    envelope = result.envelope
    assert envelope.outcome == "agent_failed"
    assert envelope.error is not None and envelope.error.startswith("container:")
    assert envelope.session_id is None
    assert envelope.agent_duration_ms is None
    assert result.texts == [AGENT_FAILURE_NOTICE]


async def test_envelope_outcome_silence(tmp_path: Path) -> None:
    """A deliberate group silence is distinguishable from a failure."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch("alice_office_router.core.peek_observed", return_value=[]),
        patch("alice_office_router.core.clear_observed"),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_C1:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="NO_REPLY")),
        ),
    ):
        result = await process_inbound(_group_msg(), settings)

    assert result.texts == []
    assert result.envelope.outcome == "silence"
    assert result.envelope.error is None
    assert result.envelope.session_id == "line_C1"
    # The message DID reach Hermes, which recorded it — the envelope must not
    # keep a second copy of the text (conversation_store.UNRECORDED_OUTCOMES).
    assert result.envelope.inbound_text is None


async def test_timeout_error_names_the_exception_type_not_its_empty_message(
    tmp_path: Path,
) -> None:
    """httpx timeouts stringify to "" — the error field must still say something."""
    import httpx

    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=httpx.ReadTimeout("")),
        ),
    ):
        result = await process_inbound(_msg(), settings)

    assert result.envelope.error == "agent: ReadTimeout"
    # A timeout gets its own wording: the agent keeps going, so asking again is cheap.
    assert result.texts == [AGENT_TIMEOUT_NOTICE]


async def test_ceiling_timeout_error_gets_the_timeout_notice(tmp_path: Path) -> None:
    """The absolute ceiling raises TimeoutError, not an httpx error — same notice."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=TimeoutError()),
        ),
    ):
        result = await process_inbound(_msg(), settings)

    assert result.envelope.error == "agent: TimeoutError"
    assert result.envelope.outcome == "agent_failed"
    assert result.texts == [AGENT_TIMEOUT_NOTICE]


async def test_agent_reported_failure_gets_the_generic_failure_notice(tmp_path: Path) -> None:
    """A Hermes-reported failed turn is not a timeout: generic notice, reason logged."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("Hermes agent failed: tool crashed")),
        ),
    ):
        result = await process_inbound(_msg(), settings)

    assert result.envelope.error == "agent: ValueError: Hermes agent failed: tool crashed"
    assert result.texts == [AGENT_FAILURE_NOTICE]


async def test_validation_error_never_carries_the_rejected_input(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """pydantic embeds the value it refused — here, the agent's reply text."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)
    # AgentReply.text is a str, so an int fails validation and the reply text
    # this stands in for would otherwise be quoted into the exception's str().
    with pytest.raises(ValidationError) as excinfo:
        AgentReply(text={"leaked": "s3cret-reply-text"})  # type: ignore[arg-type]
    failure = excinfo.value

    with (
        caplog.at_level(logging.ERROR),
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock(side_effect=failure)),
    ):
        result = await process_inbound(_msg(), settings)

    error = result.envelope.error
    assert error is not None
    assert error.startswith("agent: ValidationError")
    assert "text" in error
    assert "s3cret-reply-text" not in error
    assert not any("s3cret-reply-text" in record.getMessage() for record in caplog.records)


async def test_envelope_records_rotation_and_request_context(tmp_path: Path) -> None:
    """A rotating turn flags it, and the envelope inherits the bound request/event ids."""
    from structlog.contextvars import bound_contextvars

    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path, SESSION_IDLE_RESET_MINUTES=1440)
    _seed_session(
        settings,
        "line_room_AAA",
        SessionState(epoch=1, last_activity_ts=time.time() - 3 * 24 * 60 * 60),
    )
    fake_ask, _ = _recording_ask(lambda session_id: AgentReply(text="新回覆"))

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=fake_ask),
        bound_contextvars(request_id="req-1", event_id="evt-1"),
    ):
        result = await process_inbound(_msg("今天的進度"), settings)

    envelope = result.envelope
    assert envelope.rotated is True
    assert envelope.session_id == "line_room_AAA#2"
    assert envelope.request_id == "req-1"
    assert envelope.event_id == "evt-1"


async def test_envelope_has_no_request_context_outside_a_request(tmp_path: Path) -> None:
    """Outside an HTTP request the ids are simply absent, not an error."""
    from structlog.contextvars import clear_contextvars

    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)
    clear_contextvars()

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value=AgentReply(text="回覆")),
        ),
    ):
        result = await process_inbound(_msg(), settings)

    assert result.envelope.request_id is None
    assert result.envelope.event_id is None


# ---------------------------------------------------------------------------
# process_inbound — one agent turn at a time per room
# ---------------------------------------------------------------------------


@pytest.fixture
def room_locks() -> Iterator[dict[str, asyncio.Lock]]:
    """Give each serialization test a clean per-room lock registry.

    Yields:
        core's `_room_locks` dict, emptied before and after the test so a lock
        left over from another test can never change the outcome.
    """
    from alice_office_router.core import _room_locks

    _room_locks.clear()
    yield _room_locks
    _room_locks.clear()


async def test_same_room_turns_run_one_at_a_time(
    tmp_path: Path, room_locks: dict[str, asyncio.Lock]
) -> None:
    """A second message for the same room waits for the first turn, then runs in order."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    trace: list[str] = []

    async def _ask(
        base_url: str,
        session_id: str,
        text: str,
        api_key: str,
        *,
        idle_timeout_seconds: float,
        max_seconds: float,
        system: str | None = None,
    ) -> AgentReply:
        trace.append(f"enter:{text}")
        if text == "第一則":
            first_entered.set()
            await release_first.wait()
        trace.append(f"exit:{text}")
        return AgentReply(text=f"回覆 {text}")

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=_ask),
        patch("alice_office_router.core.struct_logger", new=Mock()) as mock_struct_logger,
    ):
        first = asyncio.create_task(process_inbound(_msg("第一則"), settings))
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        second = asyncio.create_task(process_inbound(_msg("第二則"), settings))
        # The second turn must be parked on the room lock, not in the agent.
        await asyncio.sleep(0.05)
        assert trace == ["enter:第一則"]

        release_first.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert trace == ["enter:第一則", "exit:第一則", "enter:第二則", "exit:第二則"]
    assert [result.texts for result in results] == [["回覆 第一則"], ["回覆 第二則"]]
    # The wait is visible to an operator (docs/troubleshooting.md).
    mock_struct_logger.info.assert_called_once()
    event, kwargs = mock_struct_logger.info.call_args
    assert event == ("room_turn_queued",)
    assert kwargs["room_key"] == "line_room_AAA"
    assert kwargs["waited_ms"] >= 0


async def test_different_rooms_are_not_serialized(
    tmp_path: Path, room_locks: dict[str, asyncio.Lock]
) -> None:
    """Two rooms run concurrently: the second starts while the first is still blocked."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)
    both_running = asyncio.Event()
    entered: list[str] = []

    async def _ask(
        base_url: str,
        session_id: str,
        text: str,
        api_key: str,
        *,
        idle_timeout_seconds: float,
        max_seconds: float,
        system: str | None = None,
    ) -> AgentReply:
        entered.append(session_id)
        # The first room's turn only finishes once the second room's turn has
        # started, so a shared lock would deadlock this test into its timeout.
        if len(entered) < 2:
            await both_running.wait()
        else:
            both_running.set()
        return AgentReply(text="好")

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_room:8642",
        ),
        patch("alice_office_router.core.ask_hermes_agent", new=_ask),
    ):
        await asyncio.wait_for(
            asyncio.gather(
                process_inbound(_msg("甲", room_key="line_room_A"), settings),
                process_inbound(_msg("乙", room_key="line_room_B"), settings),
            ),
            timeout=2,
        )

    assert entered == ["line_room_A", "line_room_B"]


async def test_unaddressed_group_message_is_observed_while_the_lock_is_held(
    tmp_path: Path, room_locks: dict[str, asyncio.Lock]
) -> None:
    """The observe short-circuit never waits on the room lock: background keeps accruing."""
    from alice_office_router.core import process_inbound

    settings = _settings(DATA_DIR=tmp_path)
    held = room_locks.setdefault("line_C1", asyncio.Lock())
    await held.acquire()

    try:
        with patch("alice_office_router.core.record_observed") as mock_record:
            result = await asyncio.wait_for(
                process_inbound(_group_msg("閒聊一句", addressed=False), settings), timeout=2
            )
    finally:
        held.release()

    assert result.texts == []
    assert result.envelope.outcome == "observed"
    mock_record.assert_called_once_with(settings, "line_C1", "U1", "王小明", "閒聊一句")
