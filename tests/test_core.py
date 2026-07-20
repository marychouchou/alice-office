from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings
from alice_office_router.hermes_client import AgentReply
from alice_office_router.session_hygiene import RESET_CONFIRMATION, SessionState, load_state

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"

_BLOCKED_MSG = "請先授權 Google 帳號：https://example.com/oauth/start?user_id=room_aaa"
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
        base_url: str, session_id: str, text: str, api_key: str, *, system: str | None = None
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


# ---------------------------------------------------------------------------
# process_inbound — normal flow
# ---------------------------------------------------------------------------


async def test_ok_status_returns_only_agent_reply(tmp_path: Path) -> None:
    """An "ok" gate result resolves the container, asks the agent, and returns its reply."""
    from alice_office_router.core import process_inbound

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
        texts = await process_inbound(_msg(), settings)

    mock_get_container.assert_called_once_with("line_room_AAA", settings)
    # Epoch 0 (fresh room, no rotation) sends the bare room_key and no handoff,
    # so the request is byte-identical to the legacy 1:1 path (system=None);
    # only the internal call signature grew (session_id vs room_id).
    mock_ask.assert_awaited_once_with(
        "http://hermes_line_room_AAA:8642",
        "line_room_AAA",
        "哈囉",
        "test_api_server_key",
        system=None,
    )
    assert texts == ["哈囉，我是 Hermes"]
    # Epoch 0 stays 0 with no trigger — backward compatible with existing sessions.
    assert load_state(settings, "line_room_AAA").epoch == 0


# ---------------------------------------------------------------------------
# process_inbound — Google OAuth gate
# ---------------------------------------------------------------------------


async def test_blocked_returns_auth_message_and_never_calls_agent() -> None:
    """A "blocked" gate result returns only the auth message and skips the agent."""
    from alice_office_router.core import process_inbound

    settings = _settings()

    with (
        patch(
            "alice_office_router.core.check_google_authorization",
            return_value=("blocked", _BLOCKED_MSG),
        ),
        patch("alice_office_router.core.get_or_create_container") as mock_get_container,
        patch("alice_office_router.core.ask_hermes_agent", new=AsyncMock()) as mock_ask,
    ):
        texts = await process_inbound(_msg(), settings)

    mock_get_container.assert_not_called()
    mock_ask.assert_not_awaited()
    assert len(texts) == 1
    assert "oauth/start" in texts[0]


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
        texts = await process_inbound(_msg(), settings)

    mock_get_container.assert_called_once_with("line_room_AAA", settings)
    assert len(texts) == 2
    assert "oauth/start" in texts[0]
    assert texts[1] == "哈囉，我是 Hermes"


# ---------------------------------------------------------------------------
# process_inbound — downstream failure handling
# ---------------------------------------------------------------------------


async def test_agent_error_returns_no_texts(tmp_path: Path) -> None:
    """A Hermes agent failure is swallowed; process_inbound returns no texts."""
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
        texts = await process_inbound(_msg(), settings)

    assert texts == []


async def test_container_error_returns_no_texts_and_skips_agent() -> None:
    """A container failure is swallowed, the agent is never asked, and no texts return."""
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
        texts = await process_inbound(_msg(), settings)

    mock_ask.assert_not_awaited()
    assert texts == []


async def test_notice_kept_when_agent_fails(tmp_path: Path) -> None:
    """When the agent fails after a notice, the notice is still returned on its own."""
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
        texts = await process_inbound(_msg(), settings)

    assert len(texts) == 1
    assert "Drive" in texts[0]


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
        texts = await process_inbound(_group_msg("userA 跟 userB 問早", addressed=False), settings)

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
        texts = await process_inbound(_group_msg(), settings)

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
    """When the group agent call fails, the observed buffer is not cleared."""
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
        texts = await process_inbound(_group_msg(), settings)

    assert texts == []
    mock_clear.assert_not_called()


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
        texts = await process_inbound(_group_msg(), settings)

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
        texts = await process_inbound(_msg("/new"), settings)

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
        texts = await process_inbound(_group_msg("/new"), settings)

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
        texts = await process_inbound(_msg("今天的進度"), settings)

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
        texts = await process_inbound(_msg("今天的進度"), settings)

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
        texts = await process_inbound(_group_msg(), settings)

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
