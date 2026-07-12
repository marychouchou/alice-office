from __future__ import annotations

from unittest.mock import AsyncMock, patch

from alice_office_router.channels.base import InboundMessage
from alice_office_router.channels.pipeline import process_inbound
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


class RecordingResponder:
    """Test double for the Responder protocol: records every delivery."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.notices: list[str] = []

    async def send_reply(self, text: str) -> None:
        self.replies.append(text)

    async def send_notice(self, text: str) -> None:
        self.notices.append(text)


def _message(room_id: str = "room_AAA", text: str = "哈囉") -> InboundMessage:
    """Build an InboundMessage for pipeline tests.

    Args:
        room_id: Room id to use (defaults to a safe test id).
        text: Message text.

    Returns:
        An InboundMessage on a synthetic test channel.
    """
    return InboundMessage(channel="test", room_id=room_id, text=text)


# ---------------------------------------------------------------------------
# Normal flow
# ---------------------------------------------------------------------------


async def test_delivers_agent_reply_and_returns_replied() -> None:
    """The pipeline resolves the container, asks the agent, and delivers the reply."""
    settings = _settings()
    responder = RecordingResponder()

    with (
        patch(
            "alice_office_router.channels.pipeline.get_or_create_container",
            return_value="http://hermes_room_AAA:8642",
        ) as mock_get_container,
        patch(
            "alice_office_router.channels.pipeline.ask_hermes_agent",
            new=AsyncMock(return_value="哈囉，我是 Hermes"),
        ) as mock_ask,
    ):
        outcome = await process_inbound(_message(), responder, settings)

    assert outcome == "replied"
    mock_get_container.assert_called_once_with("room_AAA", settings)
    mock_ask.assert_awaited_once_with(
        "http://hermes_room_AAA:8642", "room_AAA", "哈囉", "test_api_server_key"
    )
    assert responder.replies == ["哈囉，我是 Hermes"]
    assert responder.notices == []


async def test_unsafe_room_id_is_dropped_without_processing() -> None:
    """A path-traversal-style room id must never reach the container layer."""
    settings = _settings()
    responder = RecordingResponder()

    with (
        patch("alice_office_router.channels.pipeline.get_or_create_container") as mock_container,
        patch(
            "alice_office_router.channels.pipeline.ask_hermes_agent", new=AsyncMock()
        ) as mock_ask,
    ):
        outcome = await process_inbound(_message(room_id="../etc"), responder, settings)

    assert outcome == "dropped"
    mock_container.assert_not_called()
    mock_ask.assert_not_awaited()
    assert responder.replies == []


async def test_container_failure_returns_container_error() -> None:
    """A container-layer failure is logged and reported; nothing is delivered."""
    settings = _settings()
    responder = RecordingResponder()

    with patch(
        "alice_office_router.channels.pipeline.get_or_create_container",
        side_effect=RuntimeError("docker down"),
    ):
        outcome = await process_inbound(_message(), responder, settings)

    assert outcome == "container_error"
    assert responder.replies == []


async def test_agent_failure_returns_agent_error_without_delivery() -> None:
    """A Hermes agent failure must not raise or deliver anything."""
    settings = _settings()
    responder = RecordingResponder()

    with (
        patch(
            "alice_office_router.channels.pipeline.get_or_create_container",
            return_value="http://hermes_room_AAA:8642",
        ),
        patch(
            "alice_office_router.channels.pipeline.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
    ):
        outcome = await process_inbound(_message(), responder, settings)

    assert outcome == "agent_error"
    assert responder.replies == []


async def test_delivery_failure_returns_delivery_error() -> None:
    """A responder failure after a successful agent call is logged, not raised."""
    settings = _settings()
    responder = RecordingResponder()
    responder.send_reply = AsyncMock(side_effect=RuntimeError("network"))  # type: ignore[method-assign]

    with (
        patch(
            "alice_office_router.channels.pipeline.get_or_create_container",
            return_value="http://hermes_room_AAA:8642",
        ),
        patch(
            "alice_office_router.channels.pipeline.ask_hermes_agent",
            new=AsyncMock(return_value="哈囉"),
        ),
    ):
        outcome = await process_inbound(_message(), responder, settings)

    assert outcome == "delivery_error"


# ---------------------------------------------------------------------------
# Google OAuth gate
# ---------------------------------------------------------------------------


class TestGoogleGate:
    async def test_blocked_delivers_auth_message_and_never_calls_agent(self) -> None:
        settings = _settings()
        responder = RecordingResponder()

        with (
            patch(
                "alice_office_router.channels.pipeline.check_google_authorization",
                return_value=(
                    "blocked",
                    "請先授權 Google 帳號：https://example.com/oauth/start?user_id=room_aaa",
                ),
            ),
            patch(
                "alice_office_router.channels.pipeline.get_or_create_container"
            ) as mock_get_container,
            patch(
                "alice_office_router.channels.pipeline.ask_hermes_agent", new=AsyncMock()
            ) as mock_ask,
        ):
            outcome = await process_inbound(_message(), responder, settings)

        assert outcome == "blocked"
        mock_get_container.assert_not_called()
        mock_ask.assert_not_awaited()
        assert len(responder.replies) == 1
        assert "oauth/start" in responder.replies[0]

    async def test_ok_status_proceeds_with_normal_flow(self) -> None:
        settings = _settings()
        responder = RecordingResponder()

        with (
            patch(
                "alice_office_router.channels.pipeline.check_google_authorization",
                return_value=("ok", None),
            ),
            patch(
                "alice_office_router.channels.pipeline.get_or_create_container",
                return_value="http://hermes_room_AAA:8642",
            ) as mock_get_container,
            patch(
                "alice_office_router.channels.pipeline.ask_hermes_agent",
                new=AsyncMock(return_value="哈囉，我是 Hermes"),
            ),
        ):
            outcome = await process_inbound(_message(), responder, settings)

        assert outcome == "replied"
        mock_get_container.assert_called_once_with("room_AAA", settings)
        assert responder.replies == ["哈囉，我是 Hermes"]
        assert responder.notices == []

    async def test_notice_status_sends_notice_and_still_calls_agent(self) -> None:
        settings = _settings()
        responder = RecordingResponder()

        with (
            patch(
                "alice_office_router.channels.pipeline.check_google_authorization",
                return_value=(
                    "notice",
                    "缺少 Drive 授權：https://example.com/oauth/start?user_id=room_aaa",
                ),
            ),
            patch(
                "alice_office_router.channels.pipeline.get_or_create_container",
                return_value="http://hermes_room_AAA:8642",
            ) as mock_get_container,
            patch(
                "alice_office_router.channels.pipeline.ask_hermes_agent",
                new=AsyncMock(return_value="哈囉，我是 Hermes"),
            ),
        ):
            outcome = await process_inbound(_message(), responder, settings)

        assert outcome == "replied"
        mock_get_container.assert_called_once()
        assert len(responder.notices) == 1
        assert "oauth/start" in responder.notices[0]
        assert responder.replies == ["哈囉，我是 Hermes"]
