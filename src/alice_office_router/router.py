from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.config import Settings, get_settings
from alice_office_router.container_manager import CONTAINER_DATA_DIR, get_or_create_container
from alice_office_router.hermes_client import ask_hermes_agent
from alice_office_router.line_client import (
    download_line_content,
    push_line_message,
    reply_line_message,
)
from alice_office_router.line_dedup import EventDeduplicator
from alice_office_router.line_verify import verify_line_signature

logger = logging.getLogger(__name__)

router = APIRouter()

# Process-local; fine for the current single-worker deployment (see README).
_dedup = EventDeduplicator()

_INCOMING_SUBDIR = "incoming"
# LINE message.type -> file extension used when caching downloaded media,
# mirroring Hermes Agent's own LINE adapter's choices.
_MEDIA_EXTENSIONS = {"image": ".jpg", "audio": ".m4a", "video": ".mp4", "file": ".bin"}


def _resolve_room_id(event: dict[str, object]) -> str | None:
    """Resolve the room/user/group ID from a single LINE event's source, if present.

    Args:
        event: A single LINE webhook event dict.

    Returns:
        The room_id string, or None if it cannot be resolved.
    """
    source = event.get("source")
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if not isinstance(source_type, str):
        return None
    room_id = source.get(f"{source_type}Id")
    return room_id if isinstance(room_id, str) and room_id else None


def _extract_room_id(body: dict[str, object]) -> str:
    """Extract the unique room/user/group ID from a LINE webhook event body.

    Validates only the first event's shape — used as a synchronous envelope
    check so malformed webhook pings surface as 400 instead of being silently
    accepted. Per-event processing for the full events array happens
    separately in `_dispatch_event`, which degrades gracefully instead of
    raising.

    Args:
        body: Parsed JSON body of the LINE webhook request.

    Returns:
        The room_id string extracted from the first event's source.

    Raises:
        HTTPException: If the room ID cannot be extracted.
    """
    events = body.get("events")
    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=400, detail="No events in webhook body")

    event = events[0]
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid event format")

    room_id = _resolve_room_id(event)
    if room_id is None:
        raise HTTPException(status_code=400, detail="Missing source/room id in event")
    return room_id


def _resolve_media_filename(message: dict[str, object], msg_type: str, message_id: str) -> str:
    """Pick a filename for a downloaded media message.

    LINE's "file" message events carry the original upload's `fileName`
    (with its real extension), which we use as-is so the agent's own tools
    can recognize the file type (e.g. `.pdf`) instead of a generic `.bin`.
    Image/audio/video events don't carry an original filename, so those
    fall back to `<message_id><extension>`.

    Args:
        message: The LINE `message` object.
        msg_type: The LINE message type (image/audio/video/file).
        message_id: The LINE message id, used as a filename fallback.

    Returns:
        A filesystem-safe filename — any directory components in a
        supplied `fileName` are stripped to prevent path traversal.
    """
    file_name = message.get("fileName")
    if isinstance(file_name, str) and file_name.strip():
        safe_name = Path(file_name).name
        if safe_name:
            return safe_name
    return f"{message_id}{_MEDIA_EXTENSIONS.get(msg_type, '.bin')}"


async def _download_and_note_media(
    message: dict[str, object], room_id: str, config: Settings
) -> str | None:
    """Download a LINE media message's binary content into the room's shared volume.

    The room's Hermes agent container mounts the same directory at
    `CONTAINER_DATA_DIR`, so once written here the file is immediately visible
    to the agent's own file/vision/audio tools — we don't try to interpret the
    media ourselves.

    Args:
        message: The LINE `message` object (type in image/audio/video/file).
        room_id: Room identifier, used to resolve the shared volume path.
        config: Application settings (LINE token + data dir).

    Returns:
        A text notice telling the agent where to find the saved file (using
        the container-side mount path), or None if the download failed.
    """
    msg_type = message.get("type")
    message_id = message.get("id")
    if not isinstance(msg_type, str) or not isinstance(message_id, str) or not message_id:
        return None

    try:
        content = await download_line_content(message_id, config.LINE_CHANNEL_ACCESS_TOKEN)
    except ApiException as exc:
        logger.error(f"Failed to download LINE {msg_type} content {message_id}: {exc}")
        return None

    filename = _resolve_media_filename(message, msg_type, message_id)
    incoming_dir = config.DATA_DIR / room_id / _INCOMING_SUBDIR
    incoming_dir.mkdir(parents=True, exist_ok=True)
    (incoming_dir / filename).write_bytes(content)

    container_path = f"{CONTAINER_DATA_DIR}/{_INCOMING_SUBDIR}/{filename}"
    return (
        f"[使用者傳送了一個{msg_type}檔案，已存放於 {container_path}，"
        "請視需要用你的工具讀取並回覆。]"
    )


async def _resolve_inbound_text(
    event: dict[str, object], room_id: str, config: Settings
) -> str | None:
    """Turn a single LINE message event into text to forward to the Hermes agent.

    Text messages pass through as-is. Media messages (image/audio/video/file)
    are downloaded and saved into the room's shared volume, then replaced with
    a notice telling the agent where to find the file. Stickers and locations
    become short placeholder text. Anything else is skipped.

    Args:
        event: A single LINE webhook "message" event.
        room_id: The resolved room_id, used for the shared volume path.
        config: Application settings.

    Returns:
        Text to forward to the Hermes agent, or None if nothing should be sent.
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    msg_type = message.get("type")

    if msg_type == "text":
        text = message.get("text")
        return text if isinstance(text, str) and text else None

    if msg_type in _MEDIA_EXTENSIONS:
        return await _download_and_note_media(message, room_id, config)

    if msg_type == "sticker":
        keywords = message.get("keywords")
        if isinstance(keywords, list) and keywords:
            return f"[使用者傳送了貼圖：{', '.join(str(k) for k in keywords)}]"
        return "[使用者傳送了貼圖]"

    if msg_type == "location":
        title = message.get("title") or ""
        address = message.get("address") or ""
        return f"[使用者傳送了位置：{title} {address}]".strip()

    logger.info(f"Ignoring unsupported LINE message type: {msg_type!r}")
    return None


async def _dispatch_event(
    event: object, background_tasks: BackgroundTasks, config: Settings
) -> None:
    """Resolve one LINE webhook event and schedule its reply as a background task.

    Silently skips (with a log line) non-message events, duplicate deliveries,
    and events whose room/user id can't be resolved — LINE's webhook contract
    doesn't let us surface per-event failures back to the caller after the
    envelope-level 200 OK has already been promised.

    Args:
        event: A single element from the webhook body's "events" array.
        background_tasks: FastAPI background task queue.
        config: Application settings.
    """
    if not isinstance(event, dict) or event.get("type") != "message":
        return

    event_id = event.get("webhookEventId")
    if isinstance(event_id, str) and _dedup.is_duplicate(event_id):
        logger.info(f"Skipping duplicate LINE webhook event {event_id}")
        return

    room_id = _resolve_room_id(event)
    if room_id is None:
        logger.warning("Skipping LINE message event with unresolvable room id")
        return

    text = await _resolve_inbound_text(event, room_id, config)
    if text is None:
        return

    reply_token = event.get("replyToken")
    reply_token = reply_token if isinstance(reply_token, str) and reply_token else None
    background_tasks.add_task(_process_and_reply, room_id, text, config, reply_token)


async def _deliver_reply(
    room_id: str, text: str, reply_token: str | None, config: Settings
) -> None:
    """Deliver a reply to LINE, preferring the free reply token over Push.

    LINE reply tokens are single-use and expire roughly 60 seconds after the
    triggering event; since the Hermes agent call can take much longer, we
    don't pre-check a local TTL — we just try the reply and let LINE's own
    rejection (expired/used/invalid token) drive the fallback, which is more
    accurate than guessing locally.

    Args:
        room_id: Target LINE user/group/room ID (used for the Push fallback).
        text: Reply text to send.
        reply_token: Reply token from the triggering event, if any.
        config: Application settings.
    """
    if reply_token:
        try:
            await reply_line_message(reply_token, text, config.LINE_CHANNEL_ACCESS_TOKEN)
            return
        except ApiException as exc:
            logger.info(
                f"LINE reply token rejected for room {room_id} ({exc}); falling back to push"
            )

    try:
        await push_line_message(room_id, text, config.LINE_CHANNEL_ACCESS_TOKEN)
    except Exception as exc:
        logger.error(f"Failed to push LINE reply for room {room_id}: {exc}")


async def _process_and_reply(
    room_id: str, text: str, config: Settings, reply_token: str | None = None
) -> None:
    """Get/create the room's Hermes agent container, ask it for a reply, and deliver it to LINE.

    Each step is independently guarded: a failure in one is logged without
    raising, since this runs in a background task after the router has
    already returned 200 OK to LINE.

    Args:
        room_id: Unique chatroom identifier used to resolve the container and session.
        text: User message text (or media/sticker/location notice) to send to the agent.
        config: Application settings.
        reply_token: LINE reply token from the triggering event, if any.
    """
    try:
        target_url = get_or_create_container(room_id, config)
    except Exception as exc:
        logger.error(f"Failed to get/create container for room {room_id}: {exc}")
        return

    try:
        reply_text = await ask_hermes_agent(target_url, room_id, text, config.HERMES_API_SERVER_KEY)
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Hermes agent request failed for room {room_id}: {exc}")
        return

    await _deliver_reply(room_id, reply_text, reply_token, config)


@router.post("/webhook")
async def line_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Handle incoming LINE platform webhook POST requests.

    Validates the LINE HMAC-SHA256 signature, then dispatches every event in
    the batch: each message event is resolved to text (downloading and
    saving media into the room's shared volume where needed), deduplicated,
    and scheduled as a background task that asks the room's Hermes agent
    container for a reply and delivers it back to LINE. Returns HTTP 200
    immediately regardless of how many events were actually processed.

    Args:
        request: Incoming FastAPI request.
        background_tasks: FastAPI background task queue.
        settings: Application settings via dependency injection.

    Returns:
        JSON dict {"status": "ok"}.

    Raises:
        HTTPException: 400 if the signature is invalid or the first event's
            room id cannot be extracted.
    """
    raw_body = await request.body()
    signature = request.headers.get("x-line-signature", "")

    if not verify_line_signature(raw_body, signature, settings.LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=400, detail="Invalid signature")

    body: dict[str, object] = await request.json()

    events = body.get("events")
    if not isinstance(events, list) or not events:
        return {"status": "ok"}

    # Envelope-level check: the first event's shape must resolve to a room id,
    # otherwise this webhook call is malformed and we reject it outright.
    _extract_room_id(body)

    for event in events:
        await _dispatch_event(event, background_tasks, settings)

    return {"status": "ok"}
