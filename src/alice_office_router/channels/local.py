"""Local dev channel: a synchronous HTTP endpoint for TUI/mobile/curl clients.

Unlike LINE (webhook in, platform API out), this channel is request/response:
the caller POSTs a message and the same HTTP response carries everything the
pipeline delivered (gate notices and the agent reply, in send order) plus the
pipeline outcome. No formatting or chunking is applied — clients get the
agent's raw text, so a TUI or mobile app can render Markdown itself.

Disabled by default: the endpoint 403s until LOCAL_CHANNEL_TOKEN is set, and
every request must present it as a Bearer token. Intended consumers today are
dev tools (scripts/chat_tui.py, curl); a future mobile app backend can start
here and graduate to its own adapter when it needs push delivery.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from alice_office_router.channels.base import InboundMessage, is_safe_room_id
from alice_office_router.channels.pipeline import PipelineOutcome, process_inbound
from alice_office_router.config import Settings, get_settings

router = APIRouter()

CHANNEL_NAME = "local"


class LocalMessageRequest(BaseModel):
    """Request body for POST /channels/local/messages."""

    room_id: str = Field(
        description=(
            "Target room. Use a fresh id (e.g. local_dev_mary) for a sandbox "
            "room, or an existing room's id to talk to that room's agent."
        )
    )
    text: str = Field(min_length=1, description="Message text to send to the agent.")

    @field_validator("room_id")
    @classmethod
    def _validate_room_id(cls, value: str) -> str:
        """Reject room ids that are unsafe as container names / path segments.

        Args:
            value: The raw room_id from the request body.

        Returns:
            The validated room_id, unchanged.

        Raises:
            ValueError: If the id fails `is_safe_room_id` (surfaces as 422).
        """
        if not is_safe_room_id(value):
            raise ValueError("room_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        return value


class LocalMessageResponse(BaseModel):
    """Response body: everything the pipeline delivered for this message."""

    status: PipelineOutcome
    messages: list[str] = Field(
        description="Delivered texts in send order (gate notices, then the agent reply)."
    )


class CollectingResponder:
    """Responder that collects deliveries for the synchronous HTTP response."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_reply(self, text: str) -> None:
        """Collect the agent reply / gate-blocked message."""
        self.messages.append(text)

    async def send_notice(self, text: str) -> None:
        """Collect a side notice (e.g. a Google OAuth link)."""
        self.messages.append(text)


def _authorize(settings: Settings, authorization: str | None) -> None:
    """Enforce the local channel's Bearer token.

    Args:
        settings: Application settings.
        authorization: Raw Authorization header value, if any.

    Raises:
        HTTPException: 403 when the channel is disabled (no token configured);
            401 when the presented token is missing or wrong.
    """
    expected = settings.LOCAL_CHANNEL_TOKEN
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="Local channel is disabled; set LOCAL_CHANNEL_TOKEN to enable it",
        )
    provided = ""
    if authorization and authorization.startswith("Bearer "):
        provided = authorization.removeprefix("Bearer ")
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid local channel token")


@router.post("/channels/local/messages")
async def local_channel_message(
    body: LocalMessageRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> LocalMessageResponse:
    """Send one message through the pipeline and return everything it delivered.

    Synchronous by design: the response waits for the agent (and, on first
    contact with a room, for its container to boot), so clients should use a
    generous read timeout (scripts/chat_tui.py uses 300s).

    Args:
        body: Room id and message text.
        settings: Application settings via dependency injection.
        authorization: Bearer token matching LOCAL_CHANNEL_TOKEN.

    Returns:
        Pipeline outcome plus all delivered texts in send order. A non-
        "replied"/"blocked" status with empty messages means the failure
        details are in the router log.

    Raises:
        HTTPException: 403 if the channel is disabled, 401 on a bad token.
    """
    _authorize(settings, authorization)
    responder = CollectingResponder()
    message = InboundMessage(channel=CHANNEL_NAME, room_id=body.room_id, text=body.text)
    outcome = await process_inbound(message, responder, settings)
    return LocalMessageResponse(status=outcome, messages=responder.messages)
