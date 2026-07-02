from __future__ import annotations

from unittest.mock import AsyncMock, patch

from alice_office_router.line_client import push_line_message


async def test_push_line_message_calls_messaging_api() -> None:
    """push_line_message sends a PushMessageRequest with the target id and text."""
    mock_push = AsyncMock()
    with patch(
        "alice_office_router.line_client.AsyncMessagingApi.push_message", new=mock_push
    ):
        await push_line_message("room_AAA", "哈囉！", "test_channel_token")

    mock_push.assert_awaited_once()
    request = mock_push.call_args.args[0]
    assert request.to == "room_AAA"
    assert len(request.messages) == 1
    assert request.messages[0].text == "哈囉！"
