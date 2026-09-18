from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.channels.line.client import (
    build_configuration,
    download_line_content,
    push_line_message,
    reply_line_message,
    show_loading_animation,
)

_CLIENT = "alice_office_router.channels.line.client"


async def test_push_line_message_calls_messaging_api() -> None:
    """push_line_message sends a PushMessageRequest with the target id and text."""
    mock_push = AsyncMock()
    with patch(
        "alice_office_router.channels.line.client.AsyncMessagingApi.push_message", new=mock_push
    ):
        await push_line_message("room_AAA", "哈囉！", "test_channel_token")

    mock_push.assert_awaited_once()
    request = mock_push.call_args.args[0]
    assert request.to == "room_AAA"
    assert len(request.messages) == 1
    assert request.messages[0].text == "哈囉！"


async def test_push_line_message_strips_markdown_before_sending() -> None:
    mock_push = AsyncMock()
    with patch(
        "alice_office_router.channels.line.client.AsyncMessagingApi.push_message", new=mock_push
    ):
        await push_line_message("room_AAA", "**重要**訊息", "test_channel_token")

    request = mock_push.call_args.args[0]
    assert request.messages[0].text == "重要訊息"


async def test_push_line_message_skips_api_call_for_blank_text() -> None:
    """No LINE API call is made when there is nothing to send."""
    mock_push = AsyncMock()
    with patch(
        "alice_office_router.channels.line.client.AsyncMessagingApi.push_message", new=mock_push
    ):
        await push_line_message("room_AAA", "", "test_channel_token")

    mock_push.assert_not_called()


async def test_reply_line_message_calls_reply_api_with_token() -> None:
    """reply_line_message sends a ReplyMessageRequest carrying the reply token and text."""
    mock_reply = AsyncMock()
    with patch(
        "alice_office_router.channels.line.client.AsyncMessagingApi.reply_message", new=mock_reply
    ):
        await reply_line_message("reply_token_123", "哈囉！", "test_channel_token")

    mock_reply.assert_awaited_once()
    request = mock_reply.call_args.args[0]
    assert request.reply_token == "reply_token_123"
    assert len(request.messages) == 1
    assert request.messages[0].text == "哈囉！"


async def test_reply_line_message_skips_api_call_for_blank_text() -> None:
    mock_reply = AsyncMock()
    with patch(
        "alice_office_router.channels.line.client.AsyncMessagingApi.reply_message", new=mock_reply
    ):
        await reply_line_message("reply_token_123", "", "test_channel_token")

    mock_reply.assert_not_called()


async def test_download_line_content_returns_bytes() -> None:
    """download_line_content fetches the message blob and returns plain bytes."""
    mock_get_content = AsyncMock(return_value=bytearray(b"binary-data"))
    with patch(
        "alice_office_router.channels.line.client.AsyncMessagingApiBlob.get_message_content",
        new=mock_get_content,
    ):
        content = await download_line_content("message_id_123", "test_channel_token")

    mock_get_content.assert_awaited_once_with("message_id_123")
    assert content == b"binary-data"
    assert isinstance(content, bytes)


# ---------------------------------------------------------------------------
# show_loading_animation — 1:1 loading indicator (cosmetic, never raises)
# ---------------------------------------------------------------------------


async def test_show_loading_animation_sends_chat_id_and_seconds() -> None:
    """The SDK gets the bare user id and the requested duration."""
    mock_show = AsyncMock()
    with patch(
        "alice_office_router.channels.line.client.AsyncMessagingApi.show_loading_animation",
        new=mock_show,
    ):
        await show_loading_animation("U123", "test_channel_token", 60)

    mock_show.assert_awaited_once()
    request = mock_show.call_args.args[0]
    assert request.chat_id == "U123"
    assert request.loading_seconds == 60


async def test_show_loading_animation_defaults_to_sixty_seconds() -> None:
    mock_show = AsyncMock()
    with patch(
        "alice_office_router.channels.line.client.AsyncMessagingApi.show_loading_animation",
        new=mock_show,
    ):
        await show_loading_animation("U123", "test_channel_token")

    assert mock_show.call_args.args[0].loading_seconds == 60


async def test_show_loading_animation_swallows_api_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An API rejection is logged at warning level and never raised."""
    mock_show = AsyncMock(side_effect=ApiException(status=400, reason="Bad Request"))
    with (
        patch(
            "alice_office_router.channels.line.client.AsyncMessagingApi.show_loading_animation",
            new=mock_show,
        ),
        caplog.at_level(logging.WARNING, logger="alice_office_router.channels.line.client"),
    ):
        await show_loading_animation("U123", "test_channel_token")

    assert any(
        record.levelno == logging.WARNING and "U123" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# build_configuration — LINE_API_BASE_URL redirection (docs/testing-paths.md)
# ---------------------------------------------------------------------------


def test_build_configuration_leaves_sdk_hosts_alone_by_default() -> None:
    """No base URL means host stays None, so the SDK picks its own LINE hosts."""
    configuration = build_configuration("test_channel_token")

    assert configuration.host is None
    assert configuration.access_token == "test_channel_token"


def test_build_configuration_uses_api_base_url_when_set() -> None:
    """A base URL (the local stub) overrides the SDK's host for every call."""
    configuration = build_configuration("test_channel_token", "http://localhost:8099")

    assert configuration.host == "http://localhost:8099"


def test_build_configuration_treats_empty_base_url_as_unset() -> None:
    """An empty string must not become the host — that would break every URL."""
    configuration = build_configuration("test_channel_token", "")

    assert configuration.host is None


async def test_push_line_message_threads_api_base_url_into_configuration() -> None:
    """The caller's base URL reaches the SDK configuration the request uses."""
    spy = MagicMock(wraps=build_configuration)
    with (
        patch(f"{_CLIENT}.build_configuration", new=spy),
        patch(f"{_CLIENT}.AsyncMessagingApi.push_message", new=AsyncMock()),
    ):
        await push_line_message("room_AAA", "哈囉！", "test_channel_token", "http://localhost:8099")

    spy.assert_called_once_with("test_channel_token", "http://localhost:8099")


async def test_reply_line_message_threads_api_base_url_into_configuration() -> None:
    """Same for the reply path, which is what an e2e test usually exercises."""
    spy = MagicMock(wraps=build_configuration)
    with (
        patch(f"{_CLIENT}.build_configuration", new=spy),
        patch(f"{_CLIENT}.AsyncMessagingApi.reply_message", new=AsyncMock()),
    ):
        await reply_line_message("reply_token_123", "哈囉！", "tok", "http://localhost:8099")

    spy.assert_called_once_with("tok", "http://localhost:8099")
