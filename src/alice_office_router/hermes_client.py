from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 120.0


class _ChatMessage(BaseModel):
    """The `message` object inside a chat completion choice."""

    model_config = ConfigDict(extra="ignore")

    content: str | None = None


class _Choice(BaseModel):
    """One entry of an OpenAI-compatible chat completion `choices` array."""

    model_config = ConfigDict(extra="ignore")

    message: _ChatMessage | None = None


class _ChatCompletion(BaseModel):
    """Minimal view of Hermes's OpenAI-compatible chat completion response."""

    model_config = ConfigDict(extra="ignore")

    choices: list[_Choice] = Field(default_factory=list)


def _build_messages(text: str, system: str | None) -> list[dict[str, str]]:
    """Build the chat `messages` array, prepending a system message if given.

    Args:
        text: The user message content.
        system: An ephemeral system message to layer on top of the room's core
            prompt for this one turn, or None for the plain user-only request.

    Returns:
        `[user]` when `system` is None, else `[system, user]`.
    """
    user_message = {"role": "user", "content": text}
    if system is None:
        return [user_message]
    return [{"role": "system", "content": system}, user_message]


async def ask_hermes_agent(
    base_url: str, room_id: str, text: str, api_key: str, *, system: str | None = None
) -> str:
    """Send a user message to a room's Hermes agent and return its reply text.

    Uses the agent's built-in OpenAI-compatible api_server platform. The room_id
    is sent as the session id so a room's container keeps conversation continuity
    across messages.

    Args:
        base_url: Base URL of the Hermes agent container (e.g. http://hermes_room_AAA:8642).
        room_id: Unique chatroom identifier, used as the Hermes session id.
        text: User message text to send.
        api_key: Bearer token matching the container's API_SERVER_KEY.
        system: Optional ephemeral system message prepended for this one turn
            (used by the group path to carry GROUP_SYSTEM_PROMPT); None sends
            the plain user-only request, byte-identical to the 1:1 path.

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
    payload = {"messages": _build_messages(text, system)}

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{base_url}/v1/chat/completions", json=payload, headers=headers
        )
        response.raise_for_status()

    completion = _ChatCompletion.model_validate(response.json())
    if not completion.choices:
        raise ValueError("Hermes agent response had no choices")

    message = completion.choices[0].message
    content = message.content if message else None
    if not content:
        raise ValueError("Hermes agent response had no message content")

    return content
