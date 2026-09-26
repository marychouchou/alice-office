from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from alice_office_router.config import Settings

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"


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


# Also defined in tests/test_core.py (same fixture; keep in sync).
@pytest.fixture
def warmups() -> Iterator[dict[str, asyncio.Task[None]]]:
    """Give each warm-up test a clean warm-up registry.

    Yields:
        warmup's `_warmups` dict, emptied before and after the test so an
        in-flight task from another test can never change the outcome.
    """
    from alice_office_router.warmup import _warmups

    _warmups.clear()
    yield _warmups
    _warmups.clear()


@pytest.fixture
def probed() -> Iterator[set[str]]:
    """Give each warm-up test a clean agent-probe registry.

    Yields:
        warmup's `_probed` set, emptied before and after the test so a room
        another test already probed never skips this test's probe.
    """
    from alice_office_router.warmup import _probed

    _probed.clear()
    yield _probed
    _probed.clear()


async def _settle_warmups() -> None:
    """Await every in-flight container warm-up.

    Must be called *inside* the test's `patch(...)` block: the warm-up task
    resolves `warmup.get_or_create_container` only once it first runs, which is
    after the call that started it has returned, so a test that leaves the
    patch before settling would hand the real docker call to a worker thread.
    """
    from alice_office_router.warmup import _warmups

    await asyncio.gather(*_warmups.values())


# ---------------------------------------------------------------------------
# warm_room — container + agent warm-up, triggered by LINE follow/join
# ---------------------------------------------------------------------------


async def test_warm_room_starts_the_container_then_probes_the_agent(
    warmups: dict[str, asyncio.Task[None]], probed: set[str]
) -> None:
    """The warm-up runs in the background and spends one throwaway turn on the agent."""
    from alice_office_router.warmup import WARMUP_PROMPT, warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.warmup.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch("alice_office_router.warmup.ask_hermes_agent", new=AsyncMock()) as mock_ask,
        patch("alice_office_router.warmup.delete_hermes_session", new=AsyncMock()) as mock_delete,
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
    from alice_office_router.warmup import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.warmup.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.warmup.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("Hermes agent failed: x")),
        ),
        patch("alice_office_router.warmup.delete_hermes_session", new=AsyncMock()) as mock_delete,
        caplog.at_level(logging.WARNING, logger="alice_office_router.warmup"),
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
    from alice_office_router.warmup import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.warmup.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch("alice_office_router.warmup.ask_hermes_agent", new=AsyncMock()),
        patch(
            "alice_office_router.warmup.delete_hermes_session",
            new=AsyncMock(side_effect=httpx.ConnectError("x")),
        ),
        caplog.at_level(logging.WARNING, logger="alice_office_router.warmup"),
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
    from alice_office_router.warmup import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.warmup.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch("alice_office_router.warmup.ask_hermes_agent", new=AsyncMock()) as mock_ask,
        patch("alice_office_router.warmup.delete_hermes_session", new=AsyncMock()),
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
    from alice_office_router.warmup import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.warmup.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ),
        patch(
            "alice_office_router.warmup.ask_hermes_agent",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("alice_office_router.warmup.delete_hermes_session", new=AsyncMock()) as mock_delete,
        caplog.at_level(logging.ERROR, logger="alice_office_router.warmup"),
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
    from alice_office_router.warmup import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.warmup.get_or_create_container",
            side_effect=RuntimeError("did not become ready"),
        ),
        patch("alice_office_router.warmup.ask_hermes_agent", new=AsyncMock()) as mock_ask,
        patch("alice_office_router.warmup.delete_hermes_session", new=AsyncMock()),
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
    from alice_office_router.warmup import warm_room

    settings = _settings()
    release = threading.Event()

    def _slow_container(room_key: str, config: Settings) -> str:
        release.wait(2)
        return "http://hermes_line_room_AAA:8642"

    with (
        patch(
            "alice_office_router.warmup.get_or_create_container", side_effect=_slow_container
        ) as mock_get_container,
        patch("alice_office_router.warmup.ask_hermes_agent", new=AsyncMock()),
        patch("alice_office_router.warmup.delete_hermes_session", new=AsyncMock()),
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
    from alice_office_router.warmup import cancel_warmups, warm_room

    settings = _settings()
    release = threading.Event()

    def _slow_container(room_key: str, config: Settings) -> str:
        release.wait(2)
        return "http://hermes_line_room_AAA:8642"

    with (
        patch("alice_office_router.warmup.get_or_create_container", side_effect=_slow_container),
        patch("alice_office_router.warmup.ask_hermes_agent", new=AsyncMock()),
        patch("alice_office_router.warmup.delete_hermes_session", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="alice_office_router.warmup"),
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
    from alice_office_router.warmup import warm_room

    settings = _settings()

    with (
        patch(
            "alice_office_router.warmup.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch("alice_office_router.warmup.ask_hermes_agent", new=AsyncMock()),
    ):
        warm_room("line_room_AAA", settings)
        await _settle_warmups()
        warm_room("line_room_AAA", settings)
        await _settle_warmups()

    assert mock_get_container.call_count == 2
