"""The LINE channel adapter: webhook handling plus reply/push delivery.

`LineAdapter` owns everything LINE-specific that used to live in `router.py`:
it exposes a FastAPI router (mounted at `/webhooks/line`) that verifies the
LINE signature, parses the webhook envelope, dedups events, resolves each
message to text, and — after `core.process_inbound` returns the reply texts —
delivers them back with the free reply token first and Push as the fallback.
The channel-free core never sees any of this; it only receives an
`InboundMessage` and returns a `list[str]` (see docs/channel-interface-design.md).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.channels.base import InboundMessage
from alice_office_router.channels.line.client import push_line_message, reply_line_message
from alice_office_router.channels.line.dedup import EventDeduplicator
from alice_office_router.channels.line.events import Event, WebhookBody, resolve_inbound_text
from alice_office_router.channels.line.verify import verify_line_signature
from alice_office_router.config import Settings, get_settings
from alice_office_router.core import process_inbound

logger = logging.getLogger(__name__)


class LineAdapter:
    """Channel adapter for LINE Official Account webhooks.

    Parses the LINE Messaging API wire format, funnels each inbound message
    into the channel-free `core.process_inbound`, and delivers the returned
    reply texts back to the room. Dedup state is held per adapter instance,
    never lifted into core (see docs/channel-interface-design.md §5).
    """

    name: str = "line"

    def __init__(self) -> None:
        """Initialize the adapter with its own bounded webhook-event dedup."""
        # Process-local; fine for the current single-worker deployment (README).
        self._dedup = EventDeduplicator()

    def api_router(self) -> APIRouter:
        """Build the FastAPI router carrying the LINE webhook handler.

        Returns:
            An APIRouter with a single POST route (empty path) so it resolves
            to exactly its mount prefix, e.g. `/webhooks/line` (and the legacy
            `/webhook` alias). Signature verification, envelope checks, dedup,
            event dispatch, and reply delivery all happen inside it.
        """
        router = APIRouter()

        @router.post("")
        async def line_webhook(
            request: Request,
            background_tasks: BackgroundTasks,
            settings: Annotated[Settings, Depends(get_settings)],
        ) -> dict[str, str]:
            return await self._handle_webhook(request, background_tasks, settings)

        return router

    async def _handle_webhook(
        self, request: Request, background_tasks: BackgroundTasks, settings: Settings
    ) -> dict[str, str]:
        """Verify, parse, and dispatch every event in one LINE webhook POST.

        Validates the LINE HMAC-SHA256 signature, then dispatches each event
        in the batch (resolving text, downloading media, deduplicating, and
        scheduling a background reply task). Returns HTTP 200 immediately
        regardless of how many events were actually processed.

        Args:
            request: Incoming FastAPI request.
            background_tasks: FastAPI background task queue.
            settings: Application settings for this request.

        Returns:
            JSON dict {"status": "ok"}.

        Raises:
            HTTPException: 400 if the signature is invalid or the first event
                has no resolvable room id.
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
            await self._dispatch_event(event, background_tasks, settings)
        return {"status": "ok"}

    async def _dispatch_event(
        self, event: Event, background_tasks: BackgroundTasks, config: Settings
    ) -> None:
        """Resolve one LINE webhook event and schedule its reply in the background.

        Silently skips (with a log line) non-message events, duplicate
        deliveries, and events whose room/user id can't be resolved — LINE's
        webhook contract doesn't let us surface per-event failures after the
        envelope-level 200 OK has already been promised.

        Args:
            event: A single (already-validated) LINE webhook event.
            background_tasks: FastAPI background task queue.
            config: Application settings.
        """
        if event.type != "message":
            return

        event_id = event.webhookEventId
        if event_id and self._dedup.is_duplicate(event_id):
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
        background_tasks.add_task(self._process_and_reply, room_id, text, config, reply_token)

    async def _process_and_reply(
        self, room_id: str, text: str, config: Settings, reply_token: str | None = None
    ) -> None:
        """Run one inbound LINE message through core and deliver its replies.

        Builds the channel-free InboundMessage, runs core.process_inbound
        (Google gate -> container -> agent), and delivers each returned text
        back to LINE. Runs in a background task after the router already
        returned 200 OK, so core's own per-step error guards keep any failure
        from raising here.

        Args:
            room_id: Unique chatroom identifier (bare LINE id in Phase 2).
            text: User message text (or media/sticker/location notice).
            config: Application settings.
            reply_token: LINE reply token from the triggering event, if any.
        """
        msg = InboundMessage(channel=self.name, room_key=room_id, text=text)
        texts = await process_inbound(msg, config)
        await self._deliver_texts(room_id, texts, reply_token, config)

    async def _deliver_texts(
        self, room_id: str, texts: list[str], reply_token: str | None, config: Settings
    ) -> None:
        """Deliver core's ordered reply texts back to the LINE room.

        The first text may use the single-use reply token (falling back to
        Push when it's expired/used, via _deliver_reply); every later text is
        pushed, since a reply token can only answer once.

        Args:
            room_id: Target LINE user/group/room id.
            texts: Reply texts from core.process_inbound, in delivery order.
            reply_token: Reply token from the triggering event, if any.
            config: Application settings.
        """
        for index, text in enumerate(texts):
            token = reply_token if index == 0 else None
            await self._deliver_reply(room_id, text, token, config)

    async def _deliver_reply(
        self, room_id: str, text: str, reply_token: str | None, config: Settings
    ) -> None:
        """Deliver a reply to LINE, preferring the free reply token over Push.

        LINE reply tokens are single-use and expire roughly 60 seconds after
        the triggering event; since the Hermes agent call can take much longer,
        we don't pre-check a local TTL — we try the reply and let LINE's own
        rejection (expired/used/invalid token) drive the fallback, which is
        more accurate than guessing locally.

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
