from __future__ import annotations

import logging

from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    AsyncMessagingApiBlob,
    Configuration,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

from alice_office_router.line_format import format_for_line

logger = logging.getLogger(__name__)


def _build_text_messages(text: str) -> list[TextMessage]:
    """Format free-form reply text into LINE-ready text bubbles.

    Strips Markdown LINE can't render and splits long text into multiple
    bubbles within LINE's per-bubble and per-call limits.

    Args:
        text: Raw reply text (may contain Markdown, may exceed one bubble).

    Returns:
        List of TextMessage objects ready to send; empty if `text` is blank.
    """
    return [TextMessage(text=chunk) for chunk in format_for_line(text)]


async def push_line_message(to: str, text: str, channel_access_token: str) -> None:
    """Push a text reply back to a LINE user, group, or room.

    Args:
        to: Target LINE user/group/room ID to push the message to.
        text: Message text to send.
        channel_access_token: LINE channel access token for authentication.

    Raises:
        linebot.v3.messaging.exceptions.ApiException: If the LINE API rejects the request.
    """
    messages = _build_text_messages(text)
    if not messages:
        return
    configuration = Configuration(access_token=channel_access_token)
    async with AsyncApiClient(configuration) as api_client:
        messaging_api = AsyncMessagingApi(api_client)
        await messaging_api.push_message(PushMessageRequest(to=to, messages=messages))


async def reply_line_message(reply_token: str, text: str, channel_access_token: str) -> None:
    """Reply to a LINE event using its single-use reply token.

    Reply tokens are free (unlike Push, which is metered) but expire roughly
    60 seconds after the triggering event and can only be used once.

    Args:
        reply_token: The `replyToken` from the triggering webhook event.
        text: Message text to send.
        channel_access_token: LINE channel access token for authentication.

    Raises:
        linebot.v3.messaging.exceptions.ApiException: If the token is invalid,
            expired, already used, or the LINE API otherwise rejects the request.
    """
    messages = _build_text_messages(text)
    if not messages:
        return
    configuration = Configuration(access_token=channel_access_token)
    async with AsyncApiClient(configuration) as api_client:
        messaging_api = AsyncMessagingApi(api_client)
        await messaging_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


async def download_line_content(message_id: str, channel_access_token: str) -> bytes:
    """Download the binary content of an inbound LINE media message.

    Args:
        message_id: The `message.id` of an image/audio/video/file message.
        channel_access_token: LINE channel access token for authentication.

    Returns:
        Raw binary content of the media message.

    Raises:
        linebot.v3.messaging.exceptions.ApiException: If the LINE API rejects the request.
    """
    configuration = Configuration(access_token=channel_access_token)
    async with AsyncApiClient(configuration) as api_client:
        blob_api = AsyncMessagingApiBlob(api_client)
        content = await blob_api.get_message_content(message_id)
    return bytes(content)
