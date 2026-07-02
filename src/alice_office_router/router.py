from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from alice_office_router.config import Settings, get_settings
from alice_office_router.container_manager import get_or_create_container
from alice_office_router.hermes_client import ask_hermes_agent
from alice_office_router.line_client import push_line_message
from alice_office_router.line_verify import verify_line_signature

logger = logging.getLogger(__name__)

router = APIRouter()


def _extract_room_id(body: dict[str, object]) -> str:
    """Extract the unique room/user/group ID from a LINE webhook event body.

    Reads the first event's source type and returns the corresponding ID field.

    Args:
        body: Parsed JSON body of the LINE webhook request.

    Returns:
        The room_id string extracted from the event source.

    Raises:
        HTTPException: If the room ID cannot be extracted.
    """
    events = body.get("events")
    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=400, detail="No events in webhook body")

    event = events[0]
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid event format")

    source = event.get("source")
    if not isinstance(source, dict):
        raise HTTPException(status_code=400, detail="Missing event source")

    source_type = source.get("type")
    if not isinstance(source_type, str):
        raise HTTPException(status_code=400, detail="Missing source type")

    room_id = source.get(f"{source_type}Id")
    if not isinstance(room_id, str) or not room_id:
        raise HTTPException(status_code=400, detail=f"Missing {source_type}Id in source")

    return room_id


def _extract_message_text(body: dict[str, object]) -> str | None:
    """Extract the text of the first event, if it is a text message.

    Args:
        body: Parsed JSON body of the LINE webhook request.

    Returns:
        The message text, or None if the first event is not a text message.
    """
    events = body.get("events")
    if not isinstance(events, list) or not events:
        return None

    event = events[0]
    if not isinstance(event, dict) or event.get("type") != "message":
        return None

    message = event.get("message")
    if not isinstance(message, dict) or message.get("type") != "text":
        return None

    text = message.get("text")
    return text if isinstance(text, str) else None


async def _process_and_reply(room_id: str, text: str, config: Settings) -> None:
    """Get/create the room's Hermes agent container, ask it for a reply, and push it to LINE.

    Each step is independently guarded: a failure in one is logged without
    raising, since this runs in a background task after the router has
    already returned 200 OK to LINE.

    Args:
        room_id: Unique chatroom identifier used to resolve the container and session.
        text: User message text to send to the agent.
        config: Application settings.
    """
    try:
        target_url = get_or_create_container(room_id, config)
    except Exception as exc:
        logger.error(f"Failed to get/create container for room {room_id}: {exc}")
        return

    try:
        reply_text = await ask_hermes_agent(
            target_url, room_id, text, config.HERMES_API_SERVER_KEY
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Hermes agent request failed for room {room_id}: {exc}")
        return

    try:
        await push_line_message(room_id, reply_text, config.LINE_CHANNEL_ACCESS_TOKEN)
    except Exception as exc:
        logger.error(f"Failed to push LINE reply for room {room_id}: {exc}")


@router.post("/webhook")
async def line_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Handle incoming LINE platform webhook POST requests.

    Validates the LINE HMAC-SHA256 signature, extracts the room ID and message
    text from the event, schedules a background task to get/create the room's
    Hermes agent container and push its reply back to LINE, and immediately
    returns HTTP 200 to LINE.

    Args:
        request: Incoming FastAPI request.
        background_tasks: FastAPI background task queue.
        settings: Application settings via dependency injection.

    Returns:
        JSON dict {"status": "ok"}.

    Raises:
        HTTPException: 400 if signature is invalid or room_id cannot be extracted.
    """
    raw_body = await request.body()
    signature = request.headers.get("x-line-signature", "")

    if not verify_line_signature(raw_body, signature, settings.LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=400, detail="Invalid signature")

    body: dict[str, object] = await request.json()

    events = body.get("events")
    if not isinstance(events, list) or not events:
        return {"status": "ok"}

    room_id = _extract_room_id(body)
    text = _extract_message_text(body)
    if text is not None:
        background_tasks.add_task(_process_and_reply, room_id, text, settings)

    return {"status": "ok"}
