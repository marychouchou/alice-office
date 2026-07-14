from __future__ import annotations

from unittest.mock import AsyncMock, patch

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings

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


def _msg(text: str = "哈囉", room_key: str = "line_room_AAA") -> InboundMessage:
    """Build a channel-free InboundMessage for the tests.

    Args:
        text: The inbound plain text.
        room_key: The room key core routes on.

    Returns:
        An InboundMessage tagged with the "line" channel.
    """
    return InboundMessage(channel="line", room_key=room_key, text=text)


# ---------------------------------------------------------------------------
# process_inbound — normal flow
# ---------------------------------------------------------------------------


async def test_ok_status_returns_only_agent_reply() -> None:
    """An "ok" gate result resolves the container, asks the agent, and returns its reply."""
    from alice_office_router.core import process_inbound

    settings = _settings()

    with (
        patch("alice_office_router.core.check_google_authorization", return_value=("ok", None)),
        patch(
            "alice_office_router.core.get_or_create_container",
            return_value="http://hermes_line_room_AAA:8642",
        ) as mock_get_container,
        patch(
            "alice_office_router.core.ask_hermes_agent",
            new=AsyncMock(return_value="哈囉，我是 Hermes"),
        ) as mock_ask,
    ):
        texts = await process_inbound(_msg(), settings)

    mock_get_container.assert_called_once_with("line_room_AAA", settings)
    mock_ask.assert_awaited_once_with(
        "http://hermes_line_room_AAA:8642", "line_room_AAA", "哈囉", "test_api_server_key"
    )
    assert texts == ["哈囉，我是 Hermes"]


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


async def test_notice_returns_notice_then_agent_reply() -> None:
    """A "notice" gate result returns the notice followed by the agent reply."""
    from alice_office_router.core import process_inbound

    settings = _settings()

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
            new=AsyncMock(return_value="哈囉，我是 Hermes"),
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


async def test_agent_error_returns_no_texts() -> None:
    """A Hermes agent failure is swallowed; process_inbound returns no texts."""
    from alice_office_router.core import process_inbound

    settings = _settings()

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


async def test_notice_kept_when_agent_fails() -> None:
    """When the agent fails after a notice, the notice is still returned on its own."""
    from alice_office_router.core import process_inbound

    settings = _settings()

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
