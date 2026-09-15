from __future__ import annotations

import logging

from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    AsyncMessagingApiBlob,
    Configuration,
    PushMessageRequest,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
)

from alice_office_router.channels.line.format import format_for_line

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


async def show_loading_animation(
    user_id: str, channel_access_token: str, seconds: int = 60
) -> None:
    """Show LINE's native loading animation in a one-on-one chat.

    POSTs to `/v2/bot/chat/loading/start`
    (https://developers.line.biz/en/docs/messaging-api/use-loading-indicator/).
    The animation clears itself once `seconds` elapse or the bot sends any
    message, whichever comes first; re-issuing it while one is running just
    overrides the remaining time. LINE only supports this in 1:1 chats —
    group and multi-person rooms must never be passed here.

    Purely cosmetic, so every failure (API rejection, network error) is
    logged at warning level and swallowed: a missing animation must never
    take down the reply the caller is actually waiting for.

    Args:
        user_id: Bare LINE user ID of the 1:1 chat (no channel prefix).
        channel_access_token: LINE channel access token for authentication.
        seconds: How long to show it; LINE accepts 5-60 in multiples of 5.
    """
    configuration = Configuration(access_token=channel_access_token)
    try:
        async with AsyncApiClient(configuration) as api_client:
            messaging_api = AsyncMessagingApi(api_client)
            await messaging_api.show_loading_animation(
                ShowLoadingAnimationRequest(chatId=user_id, loadingSeconds=seconds)
            )
    except Exception as exc:
        logger.warning(f"Failed to show LINE loading animation for room {user_id}: {exc}")
