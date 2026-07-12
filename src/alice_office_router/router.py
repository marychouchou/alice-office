from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.config import Settings, get_settings
from alice_office_router.container_manager import get_or_create_container
from alice_office_router.google_oauth import check_google_authorization
from alice_office_router.hermes_client import ask_hermes_agent
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


async def _apply_google_gate(room_id: str, config: Settings, reply_token: str | None) -> bool:
    """Check Google OAuth authorization for a room and act on the result.

    Args:
        room_id: Unique chatroom identifier.
        config: Application settings.
        reply_token: LINE reply token from the triggering event, if any.

    Returns:
        True if the caller should stop processing (the room is blocked
        pending authorization and its auth-link message has already been
        delivered); False to continue with the normal agent flow (either
        fully authorized, or authorized-with-notice — the notice has
        already been pushed as a side message in that case).
    """
    status, message = check_google_authorization(room_id, config)
    if status == "blocked" and message is not None:
        await _deliver_reply(room_id, message, reply_token, config)
        return True
    if status == "notice" and message is not None:
        try:
            await push_line_message(room_id, message, config.LINE_CHANNEL_ACCESS_TOKEN)
        except Exception as exc:
            logger.error(f"Failed to push Google OAuth notice for room {room_id}: {exc}")
    return False


async def _process_and_reply(
    room_id: str, text: str, config: Settings, reply_token: str | None = None
) -> None:
    """Get/create the room's Hermes agent container, ask it for a reply, and deliver it to LINE.

    Each step is independently guarded: a failure in one is logged without
    raising, since this runs in a background task after the router has
    already returned 200 OK to LINE. Before contacting the agent, checks
    whether the room still needs Google OAuth authorization — if blocked,
    the agent is never invoked and the auth link is delivered instead.

    Args:
        room_id: Unique chatroom identifier used to resolve the container and session.
        text: User message text (or media/sticker/location notice) to send to the agent.
        config: Application settings.
        reply_token: LINE reply token from the triggering event, if any.
    """
    if await _apply_google_gate(room_id, config, reply_token):
        return

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
