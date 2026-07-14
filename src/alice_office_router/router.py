from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings, get_settings
from alice_office_router.core import process_inbound
from alice_office_router.line_client import push_line_message, reply_line_message
from alice_office_router.line_dedup import EventDeduplicator
from alice_office_router.line_events import Event, WebhookBody, resolve_inbound_text
from alice_office_router.line_verify import verify_line_signature

logger = logging.getLogger(__name__)

router = APIRouter()

# Process-local; fine for the current single-worker deployment (see README).
_dedup = EventDeduplicator()


async def _dispatch_event(
    event: Event, background_tasks: BackgroundTasks, config: Settings
) -> None:
    """Resolve one LINE webhook event and schedule its reply as a background task.

    Silently skips (with a log line) non-message events, duplicate deliveries,
    and events whose room/user id can't be resolved — LINE's webhook contract
    doesn't let us surface per-event failures back to the caller after the
    envelope-level 200 OK has already been promised.

    Args:
        event: A single (already-validated) LINE webhook event.
        background_tasks: FastAPI background task queue.
        config: Application settings.
    """
    if event.type != "message":
        return

    event_id = event.webhookEventId
    if event_id and _dedup.is_duplicate(event_id):
        logger.info(f"Skipping duplicate LINE webhook event {event_id}")
        return

    room_id = event.room_id
    if room_id is None:
        logger.warning("Skipping LINE message event with unresolvable room id")
        return

    text = await resolve_inbound_text(event, room_id, config)
    if text is None:
        return

    reply_token = event.replyToken or None
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


async def _deliver_texts(
    room_id: str, texts: list[str], reply_token: str | None, config: Settings
) -> None:
    """Deliver core's ordered reply texts back to the LINE room.

    The first text may use the single-use reply token (falling back to Push
    when it's expired/used, via _deliver_reply); every later text is pushed,
    since a reply token can only answer once.

    Args:
        room_id: Target LINE user/group/room id.
        texts: Reply texts from core.process_inbound, in delivery order.
        reply_token: Reply token from the triggering event, if any.
        config: Application settings.
    """
    for index, text in enumerate(texts):
        token = reply_token if index == 0 else None
        await _deliver_reply(room_id, text, token, config)


async def _process_and_reply(
    room_id: str, text: str, config: Settings, reply_token: str | None = None
) -> None:
    """Run one inbound LINE message through core and deliver its replies to LINE.

    Builds the channel-free InboundMessage, runs core.process_inbound (Google
    gate -> container -> agent), and delivers each returned text back to LINE.
    Runs in a background task after the router already returned 200 OK, so
    core's own per-step error guards keep any failure from raising here.

    Args:
        room_id: Unique chatroom identifier (bare LINE id in Phase 1).
        text: User message text (or media/sticker/location notice).
        config: Application settings.
        reply_token: LINE reply token from the triggering event, if any.
    """
    msg = InboundMessage(channel="line", room_key=room_id, text=text)
    texts = await process_inbound(msg, config)
    await _deliver_texts(room_id, texts, reply_token, config)


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
        HTTPException: 400 if the signature is invalid or the first event has
            no resolvable room id.
    """
    raw_body = await request.body()
    signature = request.headers.get("x-line-signature", "")

    if not verify_line_signature(raw_body, signature, settings.LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=400, detail="Invalid signature")

    webhook = WebhookBody.model_validate(await request.json())
    if not webhook.events:
        return {"status": "ok"}

    # Envelope-level check: the first event must resolve to a room id,
    # otherwise this webhook call is malformed and we reject it outright.
    if webhook.events[0].room_id is None:
        raise HTTPException(status_code=400, detail="Missing source/room id in event")

    for event in webhook.events:
        await _dispatch_event(event, background_tasks, settings)

    return {"status": "ok"}
