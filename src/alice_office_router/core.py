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
from alice_office_router.group_context import (
    GROUP_SYSTEM_PROMPT,
    build_group_prompt,
    clear_observed,
    is_silence,
    peek_observed,
    record_observed,
)
from alice_office_router.hermes_client import ask_hermes_agent

logger = logging.getLogger(__name__)


async def _ask_agent(
    room_key: str, text: str, config: Settings, *, system: str | None = None
) -> str | None:
    """Resolve the room's Hermes container and ask it for a reply.

    Each step is independently guarded: a failure is logged and yields None
    (the caller then delivers nothing for it), mirroring the original
    background-task contract where a downstream error must never propagate.

    Args:
        room_key: Unique room key used to resolve the container and session.
        text: User message text to forward to the agent.
        config: Application settings.
        system: Optional ephemeral system message for this turn (the group
            path passes GROUP_SYSTEM_PROMPT); None keeps the 1:1 request plain.

    Returns:
        The agent's reply text, or None if the container or agent call failed.
    """
    try:
        target_url = get_or_create_container(room_key, config)
    except Exception as exc:
        logger.error(f"Failed to get/create container for room {room_key}: {exc}")
        return None

    try:
        return await ask_hermes_agent(
            target_url, room_key, text, config.HERMES_API_SERVER_KEY, system=system
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Hermes agent request failed for room {room_key}: {exc}")
        return None


async def _ask_group_agent(msg: InboundMessage, config: Settings) -> str | None:
    """Ask the agent for an addressed group message, managing buffer and silence.

    Folds the room's observed background into a tagged prompt (design §7), asks
    the agent under the group system message, then clears only the records that
    were folded in (a failure keeps the whole context for a retry; and any
    unaddressed message observed during the agent call survives, since only the
    peeked records are dropped — see group_context.clear_observed), and drops a
    silence-token reply.

    Args:
        msg: The addressed group inbound message.
        config: Application settings.

    Returns:
        The agent's reply, or None when the agent failed or chose to stay silent.
    """
    observed = peek_observed(config, msg.room_key)
    prompt = build_group_prompt(observed, msg)
    reply = await _ask_agent(msg.room_key, prompt, config, system=GROUP_SYSTEM_PROMPT)
    if reply is None:
        return None
    clear_observed(config, msg.room_key, observed)
    return None if is_silence(reply) else reply


async def _reply_for(msg: InboundMessage, config: Settings) -> str | None:
    """Ask the agent for a reply, taking the group path for group messages.

    Args:
        msg: The inbound message (already past the observe short-circuit, so a
            group message here is one addressed to the bot).
        config: Application settings.

    Returns:
        The agent's reply text, or None if nothing should be delivered.
    """
    if msg.is_group:
        return await _ask_group_agent(msg, config)
    return await _ask_agent(msg.room_key, msg.text, config)


async def process_inbound(msg: InboundMessage, config: Settings) -> list[str]:
    """Run the channel-free pipeline for one inbound message.

    Applies the Google OAuth gate, then (unless blocked) asks the room's Hermes
    agent for a reply. The reply texts are returned in delivery order for the
    calling adapter to send back to the room; nothing is sent from here.

    Args:
        msg: The normalized inbound message (identity + plain text).
        config: Application settings.

    Returns:
        Texts to send back to the room, in order. An unaddressed group message
        is only observed and returns nothing. Gate "blocked" returns only the
        authorization message (agent not called); "notice" returns the notice
        followed by the agent reply; "ok" returns just the agent reply. A
        container/agent failure (or a silence-token group reply) drops the
        agent reply, keeping any notice.
    """
    # Observe short-circuit, before the OAuth gate: an unaddressed group
    # message must neither ask the agent nor trigger an auth prompt; a
    # blocked room still accumulates background to carry once authorized.
    if msg.is_group and not msg.addressed:
        record_observed(config, msg.room_key, msg.sender_id, msg.sender_name, msg.text)
        return []

    status, message = check_google_authorization(msg.room_key, config)
    if status == "blocked" and message is not None:
        return [message]

    replies: list[str] = []
    if status == "notice" and message is not None:
        replies.append(message)

    reply = await _reply_for(msg, config)
    if reply is not None:
        replies.append(reply)
    return replies
