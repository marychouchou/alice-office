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
from alice_office_router.session_hygiene import (
    HANDOFF_PROMPT,
    RESET_CONFIRMATION,
    begin_turn,
    build_turn_text,
    check_reset_command,
    complete_turn,
    reset_session,
    session_id_for,
)

logger = logging.getLogger(__name__)


async def _generate_handoff(
    target_url: str, room_key: str, retired_epoch: int, config: Settings
) -> str | None:
    """Best-effort: ask the just-retired session for a one-shot handoff summary.

    Sends one extra request to the retired epoch's session id (the rotation has
    already happened in begin_turn) asking for a short summary of unfinished
    items, preferences, and in-progress tasks. Any failure is logged and
    swallowed — the new epoch then continues clean-slate, since a fresh session
    with no summary still beats an ever-growing one. The summary is never
    persisted: it exists only to be injected into this turn's user message.

    Args:
        target_url: The room's Hermes container base URL.
        room_key: The room key core routes on.
        retired_epoch: The epoch just closed (whose session is summarized).
        config: Application settings.

    Returns:
        The handoff summary text, or None when the summary request failed.
    """
    old_session_id = session_id_for(room_key, retired_epoch)
    try:
        reply = await ask_hermes_agent(
            target_url, old_session_id, HANDOFF_PROMPT, config.HERMES_API_SERVER_KEY
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            f"Handoff summary failed for room {room_key}; continuing clean-slate ({exc})"
        )
        return None
    return reply.text


async def _ask_agent(
    room_key: str, text: str, config: Settings, *, system: str | None = None
) -> str | None:
    """Resolve the room's Hermes container, rotate if due, and ask for a reply.

    Each step is independently guarded: a failure is logged and yields None
    (the caller then delivers nothing for it), mirroring the original
    background-task contract where a downstream error must never propagate.
    Session hygiene is applied here so 1:1 and group turns share it: begin_turn
    evaluates the triggers and rotates atomically (before any await); a rotated
    turn then fetches a one-shot handoff summary from the retired epoch's
    session and folds it into this turn's user text; a successful turn records
    its token watermark (see session_hygiene, including the accepted trade-offs
    of the non-persisted handoff).

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

    plan = begin_turn(config, room_key)
    # retired_epoch is set exactly when this turn rotated (see TurnPlan).
    handoff = (
        await _generate_handoff(target_url, room_key, plan.retired_epoch, config)
        if plan.retired_epoch is not None
        else None
    )

    session_id = session_id_for(room_key, plan.epoch)
    try:
        result = await ask_hermes_agent(
            target_url,
            session_id,
            build_turn_text(handoff, text),
            config.HERMES_API_SERVER_KEY,
            system=system,
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Hermes agent request failed for room {room_key}: {exc}")
        return None

    complete_turn(config, room_key, epoch=plan.epoch, prompt_tokens=result.prompt_tokens)
    return result.text


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
        is only observed and returns nothing. A manual reset command rotates the
        room's session and returns only the fixed confirmation (agent not
        called). Gate "blocked" returns only the authorization message (agent
        not called); "notice" returns the notice followed by the agent reply;
        "ok" returns just the agent reply. A container/agent failure (or a
        silence-token group reply) drops the agent reply, keeping any notice.
    """
    # Observe short-circuit, before the OAuth gate: an unaddressed group
    # message must neither ask the agent nor trigger an auth prompt; a
    # blocked room still accumulates background to carry once authorized.
    if msg.is_group and not msg.addressed:
        record_observed(config, msg.room_key, msg.sender_id, msg.sender_name, msg.text)
        return []

    # Manual session reset, before the OAuth gate: rotate to a fresh epoch (no
    # handoff — a deliberate clean slate), drop any group background so it can't
    # leak into the new epoch, and confirm without spending an agent turn.
    if check_reset_command(msg, config):
        reset_session(config, msg.room_key)
        clear_observed(config, msg.room_key, peek_observed(config, msg.room_key))
        return [RESET_CONFIRMATION]

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
