from __future__ import annotations

from unittest.mock import AsyncMock, patch

from alice_office_router.line_client import (
    download_line_content,
    push_line_message,
    reply_line_message,
)


async def test_push_line_message_calls_messaging_api() -> None:
    """push_line_message sends a PushMessageRequest with the target id and text."""
    mock_push = AsyncMock()
    with patch("alice_office_router.line_client.AsyncMessagingApi.push_message", new=mock_push):
        await push_line_message("room_AAA", "哈囉！", "test_channel_token")

    mock_push.assert_awaited_once()
    request = mock_push.call_args.args[0]
    assert request.to == "room_AAA"
    assert len(request.messages) == 1
    assert request.messages[0].text == "哈囉！"


async def test_push_line_message_strips_markdown_before_sending() -> None:
    mock_push = AsyncMock()
    with patch("alice_office_router.line_client.AsyncMessagingApi.push_message", new=mock_push):
        await push_line_message("room_AAA", "**重要**訊息", "test_channel_token")

    request = mock_push.call_args.args[0]
    assert request.messages[0].text == "重要訊息"


async def test_push_line_message_skips_api_call_for_blank_text() -> None:
    """No LINE API call is made when there is nothing to send."""
    mock_push = AsyncMock()
    with patch("alice_office_router.line_client.AsyncMessagingApi.push_message", new=mock_push):
        await push_line_message("room_AAA", "", "test_channel_token")

    mock_push.assert_not_called()


async def test_reply_line_message_calls_reply_api_with_token() -> None:
    """reply_line_message sends a ReplyMessageRequest carrying the reply token and text."""
    mock_reply = AsyncMock()
    with patch("alice_office_router.line_client.AsyncMessagingApi.reply_message", new=mock_reply):
        await reply_line_message("reply_token_123", "哈囉！", "test_channel_token")

    mock_reply.assert_awaited_once()
    request = mock_reply.call_args.args[0]
    assert request.reply_token == "reply_token_123"
    assert len(request.messages) == 1
    assert request.messages[0].text == "哈囉！"


async def test_reply_line_message_skips_api_call_for_blank_text() -> None:
    mock_reply = AsyncMock()
    with patch("alice_office_router.line_client.AsyncMessagingApi.reply_message", new=mock_reply):
        await reply_line_message("reply_token_123", "", "test_channel_token")

    mock_reply.assert_not_called()


async def test_download_line_content_returns_bytes() -> None:
    """download_line_content fetches the message blob and returns plain bytes."""
    mock_get_content = AsyncMock(return_value=bytearray(b"binary-data"))
    with patch(
        "alice_office_router.line_client.AsyncMessagingApiBlob.get_message_content",
        new=mock_get_content,
    ):
        content = await download_line_content("message_id_123", "test_channel_token")

    mock_get_content.assert_awaited_once_with("message_id_123")
    assert content == b"binary-data"
    assert isinstance(content, bytes)
