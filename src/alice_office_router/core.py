"""Channel-free orchestration: gate -> container -> agent -> reply texts.

`process_inbound` is the single entry point every channel adapter funnels into
once it has parsed its own wire format into an `InboundMessage`. It runs the
Google OAuth gate, resolves the room's Hermes agent container, asks the agent,
and *returns* the plain-text messages to send back to the room — it never
touches any channel's send API or reply tokens, so it stays directly unit
testable and reusable across adapters (see docs/channel-interface-design.md).
"""

from __future__ import annotations

import logging

import httpx

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings
from alice_office_router.container_manager import get_or_create_container
from alice_office_router.google_oauth import check_google_authorization
from alice_office_router.hermes_client import ask_hermes_agent

logger = logging.getLogger(__name__)


async def _ask_agent(room_key: str, text: str, config: Settings) -> str | None:
    """Resolve the room's Hermes container and ask it for a reply.

    Each step is independently guarded: a failure is logged and yields None
    (the caller then delivers nothing for it), mirroring the original
    background-task contract where a downstream error must never propagate.

    Args:
        room_key: Unique room key used to resolve the container and session.
        text: User message text to forward to the agent.
        config: Application settings.

    Returns:
        The agent's reply text, or None if the container or agent call failed.
    """
    try:
        target_url = get_or_create_container(room_key, config)
    except Exception as exc:
        logger.error(f"Failed to get/create container for room {room_key}: {exc}")
        return None

    try:
        return await ask_hermes_agent(target_url, room_key, text, config.HERMES_API_SERVER_KEY)
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Hermes agent request failed for room {room_key}: {exc}")
        return None


async def process_inbound(msg: InboundMessage, config: Settings) -> list[str]:
    """Run the channel-free pipeline for one inbound message.

    Applies the Google OAuth gate, then (unless blocked) asks the room's Hermes
    agent for a reply. The reply texts are returned in delivery order for the
    calling adapter to send back to the room; nothing is sent from here.

    Args:
        msg: The normalized inbound message (identity + plain text).
        config: Application settings.

    Returns:
        Texts to send back to the room, in order. Gate "blocked" returns only
        the authorization message (agent not called); "notice" returns the
        notice followed by the agent reply; "ok" returns just the agent reply.
        A container/agent failure drops the agent reply, keeping any notice.
    """
    status, message = check_google_authorization(msg.room_key, config)
    if status == "blocked" and message is not None:
        return [message]

    replies: list[str] = []
    if status == "notice" and message is not None:
        replies.append(message)

    reply = await _ask_agent(msg.room_key, msg.text, config)
    if reply is not None:
        replies.append(reply)
    return replies
