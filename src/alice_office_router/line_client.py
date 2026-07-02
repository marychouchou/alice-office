from __future__ import annotations

import logging

from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    PushMessageRequest,
    TextMessage,
)

logger = logging.getLogger(__name__)


async def push_line_message(to: str, text: str, channel_access_token: str) -> None:
    """Push a text message back to a LINE user, group, or room.

    Args:
        to: Target LINE user/group/room ID to push the message to.
        text: Message text to send.
        channel_access_token: LINE channel access token for authentication.

    Raises:
        linebot.v3.messaging.exceptions.ApiException: If the LINE API rejects the request.
    """
    configuration = Configuration(access_token=channel_access_token)
    async with AsyncApiClient(configuration) as api_client:
        messaging_api = AsyncMessagingApi(api_client)
        await messaging_api.push_message(
            PushMessageRequest(to=to, messages=[TextMessage(text=text)])
        )
