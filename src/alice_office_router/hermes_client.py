from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 120.0


async def ask_hermes_agent(base_url: str, room_id: str, text: str, api_key: str) -> str:
    """Send a user message to a room's Hermes agent and return its reply text.

    Uses the agent's built-in OpenAI-compatible api_server platform. The room_id
    is sent as the session id so a room's container keeps conversation continuity
    across messages.

    Args:
        base_url: Base URL of the Hermes agent container (e.g. http://hermes_room_AAA:8642).
        room_id: Unique chatroom identifier, used as the Hermes session id.
        text: User message text to send.
        api_key: Bearer token matching the container's API_SERVER_KEY.

    Returns:
        The assistant's reply text.

    Raises:
        httpx.HTTPError: If the request fails or the container returns a non-2xx status.
        ValueError: If the response body has no usable reply content.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Hermes-Session-Id": room_id,
    }
    payload = {"messages": [{"role": "user", "content": text}]}

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{base_url}/v1/chat/completions", json=payload, headers=headers
        )
        response.raise_for_status()

    choices = response.json().get("choices")
    if not choices:
        raise ValueError("Hermes agent response had no choices")

    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("Hermes agent response had no message content")

    return content
