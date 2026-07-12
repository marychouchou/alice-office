"""LINE channel adapter: webhook endpoint, event dispatch, and reply delivery.

Owns everything LINE-specific about getting messages into and out of the
pipeline: signature verification, webhook-redelivery dedup, resolving events
to text (via `line_events`), and delivering replies (reply-token-first with
Push fallback, via `line_client`, which also applies LINE formatting/chunking).
The LINE *wire format* itself stays in the `line_*` modules per CLAUDE.md's
routing table — this module only wires them to the channel contracts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.channels.base import InboundMessage
from alice_office_router.channels.pipeline import process_inbound
from alice_office_router.config import Settings, get_settings
from alice_office_router.line_client import push_line_message, reply_line_message
from alice_office_router.line_dedup import EventDeduplicator
from alice_office_router.line_events import Event, WebhookBody, resolve_inbound_text
from alice_office_router.line_verify import verify_line_signature

logger = logging.getLogger(__name__)

router = APIRouter()

# Process-local; fine for the current single-worker deployment (see README).
_dedup = EventDeduplicator()


@dataclass
class LineResponder:
    """Delivers pipeline output back to LINE for one triggering event.

    Prefers the event's free, single-use reply token; falls back to (metered)
    Push when the token is absent or rejected. Formatting (Markdown stripping,
    bubble chunking) happens inside `line_client`.

    Attributes:
        room_id: Target LINE user/group/room ID (Push target).
        reply_token: Reply token from the triggering event, if any. Consumed
            (set to None) on first use — LINE reply tokens are single-use.
        access_token: LINE channel access token.
    """

    room_id: str
    reply_token: str | None
    access_token: str

    async def send_reply(self, text: str) -> None:
        """Deliver the answer, reply-token-first with Push fallback.

        Reply tokens expire roughly 60 seconds after the triggering event;
        since the agent call can take much longer, we don't pre-check a local
        TTL — we just try the reply and let LINE's own rejection drive the
        fallback, which is more accurate than guessing locally.

        Args:
            text: Reply text to send.
        """
        reply_token, self.reply_token = self.reply_token, None
        if reply_token:
            try:
                await reply_line_message(reply_token, text, self.access_token)
                return
            except ApiException as exc:
                logger.info(
                    f"LINE reply token rejected for room {self.room_id} ({exc}); "
                    "falling back to push"
                )
        await push_line_message(self.room_id, text, self.access_token)

    async def send_notice(self, text: str) -> None:
        """Push a side message to the room, keeping the reply token unspent.

        Args:
            text: Notice text to send.
        """
        await push_line_message(self.room_id, text, self.access_token)


async def _dispatch_event(
    event: Event, background_tasks: BackgroundTasks, config: Settings
) -> None:
    """Resolve one LINE webhook event and schedule its pipeline run.

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

    message = InboundMessage(channel="line", room_id=room_id, text=text)
    responder = LineResponder(room_id, event.replyToken or None, config.LINE_CHANNEL_ACCESS_TOKEN)
    background_tasks.add_task(process_inbound, message, responder, config)


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
    and scheduled as a background pipeline run that asks the room's Hermes
    agent container for a reply and delivers it back to LINE. Returns HTTP
    200 immediately regardless of how many events were actually processed.

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
