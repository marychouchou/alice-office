"""Channel-agnostic inbound-message pipeline: gate → container → agent → reply.

This is the code path every channel shares. It only sees the contracts in
`base.py` — never a platform SDK — so adding a channel never touches this
module. It never raises: LINE runs it as a background task after 200 OK has
been promised, so every failure is logged and reported via the returned
outcome (which synchronous channels like `local.py` surface to their caller).
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx

from alice_office_router.channels.base import InboundMessage, Responder, is_safe_room_id
from alice_office_router.config import Settings
from alice_office_router.container_manager import get_or_create_container
from alice_office_router.google_oauth import check_google_authorization
from alice_office_router.hermes_client import ask_hermes_agent

logger = logging.getLogger(__name__)

PipelineOutcome = Literal[
    "replied",  # agent answered and the reply was delivered
    "blocked",  # Google OAuth gate blocked the room; auth link was delivered
    "dropped",  # unsafe room id — nothing was processed
    "container_error",  # room's container could not be resolved/created
    "agent_error",  # Hermes agent call failed
    "delivery_error",  # agent answered but the channel failed to deliver it
]


async def _apply_google_gate(room_id: str, responder: Responder, config: Settings) -> bool:
    """Check the room's Google OAuth status and deliver any gate message.

    Args:
        room_id: Unique chatroom identifier.
        responder: Delivery half of the triggering channel.
        config: Application settings.

    Returns:
        True if processing must stop (room blocked pending authorization,
        auth-link already delivered); False to continue with the agent flow.
    """
    status, message = check_google_authorization(room_id, config)
    if status == "blocked" and message is not None:
        try:
            await responder.send_reply(message)
        except Exception as exc:
            logger.error(f"Failed to deliver Google OAuth gate message for room {room_id}: {exc}")
        return True
    if status == "notice" and message is not None:
        try:
            await responder.send_notice(message)
        except Exception as exc:
            logger.error(f"Failed to deliver Google OAuth notice for room {room_id}: {exc}")
    return False


async def process_inbound(
    message: InboundMessage, responder: Responder, config: Settings
) -> PipelineOutcome:
    """Run one inbound message through gate → container → agent → reply.

    Args:
        message: The channel-resolved inbound message.
        responder: Delivery half of the triggering channel, bound to the room.
        config: Application settings.

    Returns:
        What happened, as a PipelineOutcome — never raises.
    """
    room_id = message.room_id
    if not is_safe_room_id(room_id):
        logger.warning(f"Dropping {message.channel} message with unsafe room id {room_id!r}")
        return "dropped"

    if await _apply_google_gate(room_id, responder, config):
        return "blocked"

    try:
        target_url = get_or_create_container(room_id, config)
    except Exception as exc:
        logger.error(f"Failed to get/create container for room {room_id}: {exc}")
        return "container_error"

    try:
        reply_text = await ask_hermes_agent(
            target_url, room_id, message.text, config.HERMES_API_SERVER_KEY
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Hermes agent request failed for room {room_id}: {exc}")
        return "agent_error"

    try:
        await responder.send_reply(reply_text)
    except Exception as exc:
        logger.error(
            f"Failed to deliver agent reply to room {room_id} via {message.channel}: {exc}"
        )
        return "delivery_error"
    return "replied"
