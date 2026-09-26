from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from alice_office_router.channels.base import InboundMessage
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
# resume_pending_auth — re-run what a member parked before authorizing
# (docs/google-auth-per-member-plan.md §3.4)
# ---------------------------------------------------------------------------


class _StubAdapter:
    """A ChannelAdapter stand-in that records (or refuses) what it is asked to resume."""

    def __init__(self, name: str = "line", error: Exception | None = None) -> None:
        self.name = name
        self.error = error
        self.resumed: list[InboundMessage] = []

    def api_router(self) -> object:
        raise NotImplementedError

    async def resume(self, msg: InboundMessage) -> None:
        self.resumed.append(msg)
        if self.error is not None:
            raise self.error


@pytest.fixture
def stub_adapter() -> Iterator[_StubAdapter]:
    """Register a stub adapter as the only channel, restoring the real ones after.

    Yields:
        The registered stub; read `stub.resumed` to see what auth_links handed it.
    """
    from alice_office_router import channels

    saved = dict(channels._adapters)
    stub = _StubAdapter()
    channels.register_adapters([stub])
    yield stub
    channels.register_adapters(list(saved.values()))


def _park(
    settings: Settings, msg: InboundMessage, member_key: str, *, ts: float | None = None
) -> Path:
    """Write a pending-auth record the way auth_links.write_pending_auth does."""
    from alice_office_router.google.auth_links import write_pending_auth

    write_pending_auth(settings, msg.room_key, member_key, msg)
    path = settings.room_pending_auth_path(msg.room_key, member_key)
    if ts is not None:
        record = json.loads(path.read_text(encoding="utf-8"))
        record["ts"] = ts
        path.write_text(json.dumps(record), encoding="utf-8")
    return path


async def test_resume_pending_auth_hands_the_parked_message_to_its_adapter(
    tmp_path: Path, stub_adapter: _StubAdapter
) -> None:
    """The member gets their answer without retyping the question."""
    from alice_office_router.google.auth_links import resume_pending_auth

    settings = _settings(DATA_DIR=tmp_path)
    msg = _msg("明天有什麼會議")
    path = _park(settings, msg, "line_room_aaa")

    await resume_pending_auth("line_room_AAA", "line_room_aaa", settings)

    assert len(stub_adapter.resumed) == 1
    resumed = stub_adapter.resumed[0]
    assert resumed.text.endswith("明天有什麼會議")
    # Identity is untouched: same room, same channel, same speaker.
    assert resumed.model_dump(exclude={"text"}) == msg.model_dump(exclude={"text"})
    # Single-shot: the record is consumed, so authorizing twice never re-asks.
    assert not path.exists()


async def test_resume_tells_the_agent_the_authorization_just_happened(
    tmp_path: Path, stub_adapter: _StubAdapter
) -> None:
    """Replayed verbatim, the question is answered from the session's "not authorized" history.

    Seen in the group e2e: the resumed turn repeated "you still need to
    authorize" without retrying a single Google tool. The system-voiced prefix
    is what makes the agent try again.
    """
    from alice_office_router.google.auth_links import resume_pending_auth

    settings = _settings(DATA_DIR=tmp_path)
    _park(settings, _msg("明天有什麼會議"), "line_room_aaa")

    await resume_pending_auth("line_room_AAA", "line_room_aaa", settings)

    assert stub_adapter.resumed[0].text == (
        "（系統：剛完成 Google 授權，請重新執行剛才的請求。）明天有什麼會議"
    )


async def test_resume_in_a_group_names_who_authorized(
    tmp_path: Path, stub_adapter: _StubAdapter
) -> None:
    """A group turn carries several people's history, so the prefix has to say whose token this is."""
    from alice_office_router.google.auth_links import resume_pending_auth

    settings = _settings(DATA_DIR=tmp_path)
    msg = _group_msg("明天有什麼會議", sender_id="U1", sender_name="王小明").model_copy(
        update={"room_key": "line_C_GROUP"}
    )
    _park(settings, msg, "u1")

    await resume_pending_auth("line_C_GROUP", "u1", settings)

    assert stub_adapter.resumed[0].text == (
        "（系統：王小明 剛完成 Google 授權，請重新執行剛才的請求。）明天有什麼會議"
    )


async def test_resume_pending_auth_does_nothing_when_no_message_is_parked(
    tmp_path: Path, stub_adapter: _StubAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    """Authorizing with nothing waiting is the normal case, not an error."""
    from alice_office_router.google.auth_links import resume_pending_auth

    with caplog.at_level(logging.INFO):
        await resume_pending_auth("line_room_AAA", "line_room_aaa", _settings(DATA_DIR=tmp_path))

    assert stub_adapter.resumed == []
    assert "auth_resume_empty" in caplog.text


async def test_resume_pending_auth_ignores_an_expired_record(
    tmp_path: Path, stub_adapter: _StubAdapter
) -> None:
    """A question parked 11 minutes ago is stale; the member has moved on."""
    from alice_office_router.google.auth_links import PENDING_AUTH_TTL_SECONDS, resume_pending_auth

    settings = _settings(DATA_DIR=tmp_path)
    path = _park(
        settings,
        _msg("明天有什麼會議"),
        "line_room_aaa",
        ts=time.time() - PENDING_AUTH_TTL_SECONDS - 60,
    )

    await resume_pending_auth("line_room_AAA", "line_room_aaa", settings)

    assert stub_adapter.resumed == []
    assert not path.exists()


async def test_resume_pending_auth_logs_an_error_for_an_unmounted_channel(
    tmp_path: Path, stub_adapter: _StubAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    """A message parked by a channel this process no longer mounts is dropped."""
    from alice_office_router.google.auth_links import resume_pending_auth

    settings = _settings(DATA_DIR=tmp_path)
    msg = InboundMessage(channel="telegram", room_key="line_room_AAA", text="明天有什麼會議")
    _park(settings, msg, "line_room_aaa")

    with caplog.at_level(logging.ERROR):
        await resume_pending_auth("line_room_AAA", "line_room_aaa", settings)

    assert stub_adapter.resumed == []
    assert "auth_resume_no_adapter" in caplog.text


async def test_resume_pending_auth_logs_an_adapter_failure_instead_of_raising(
    tmp_path: Path, stub_adapter: _StubAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    """It runs detached off the OAuth callback: every failure must end in a log line."""
    from alice_office_router.google.auth_links import resume_pending_auth

    settings = _settings(DATA_DIR=tmp_path)
    stub_adapter.error = RuntimeError("push failed")
    _park(settings, _msg("明天有什麼會議"), "line_room_aaa")

    with caplog.at_level(logging.ERROR):
        await resume_pending_auth("line_room_AAA", "line_room_aaa", settings)

    assert "auth_resume_failed" in caplog.text
    assert "RuntimeError" in caplog.text
