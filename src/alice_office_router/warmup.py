"""Room cold-start strategy: warm a room's container and agent before it is needed.

A room's first message used to pay the whole cold start in front of the user:
30–60 s to create and health-wait the container, then the Hermes process's
one-time tool-registry probe on its first chat turn. `warm_room` moves both off
the critical path. LINE's `follow` and `join` events call it the moment a room
appears; it starts the container in the background and spends one throwaway
probe turn on the agent (`_probe_agent`), deduplicated per room while in flight
(`_warmups`) and probed at most once per room per process (`_probed`).
`cancel_warmups` runs at lifespan shutdown so abandoned warm-ups end in the log.

Split out of core because it changes for a different reason than the dispatch
pipeline: when and how to warm a room is a latency policy, and nothing in a
real turn depends on it — a room that was never warmed simply pays the cold
start on its first message (agent_turn.ask_agent).
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from alice_office_router.agent_turn import elapsed_ms
from alice_office_router.config import Settings
from alice_office_router.container_manager import get_or_create_container
from alice_office_router.conversation_log import describe_error
from alice_office_router.hermes_client import ask_hermes_agent, delete_hermes_session

logger = logging.getLogger(__name__)

# In-flight warm-ups, one per room. `warm_room` starts a room's container here
# — and then warms its agent with one throwaway turn — so the room's first real
# message lands on a ready agent instead of paying the 30–60 s cold start plus
# the agent's first-turn tax. Triggered from LINE's `follow` (1:1 friend add)
# and `join` (added to a group) events — the moment a room appears, well before
# its first question; it used to hang off the Google gate's "blocked" reply,
# which no longer exists (docs/google-auth-per-member-plan.md §3.5). Keyed by
# room so a second trigger while the first warm-up is still running reuses it
# instead of spawning another thread. The entry is dropped when the task
# finishes (success or failure), so a later trigger retries a warm-up that
# failed — deduplication is in-flight only, never "once ever". Holding the Task
# also keeps it from being garbage collected mid-flight. Process-local, like
# `core._room_locks`.
_warmups: dict[str, asyncio.Task[None]] = {}

# The session id the warm-up probe turn runs under. Deliberately neither a room
# key (`line_…`/`api_…`) nor a `room_key#N` epoch id (session_hygiene.
# session_id_for), so it can never collide with a user session; the probe is
# deleted right after it runs anyway (see _probe_agent).
WARMUP_SESSION_ID = "warmup-probe"

# What the probe asks. Cheap on purpose: the point is to make the Hermes process
# run one chat turn, not to get an answer.
WARMUP_PROMPT = "Warm-up ping from the router. Reply with the single word OK and do nothing else."

# Absolute ceiling for the probe turn. A probe that takes longer than this is
# worthless — the user's real message has long since arrived by then — and
# HERMES_REQUEST_TIMEOUT_SECONDS is sized for real user turns (tool loops,
# document work), not for a one-word ping.
_WARMUP_MAX_SECONDS = 120.0

# Rooms whose agent this router process has already probed. Starting a
# container is cheap to repeat; the probe is a real ~28k-token LLM call, and a
# second warm-up trigger for the same room would land on an already-warm
# container and pay it again for nothing. The in-flight dedup in
# `_warmups` only covers warm-ups that overlap in time, so it cannot do this.
# Known misses, both accepted: an operator's `docker restart` of a room's
# container while the router stays up leaves this set stale, so that room's
# next real turn pays the tool-registry tax once; a router restart empties it,
# costing at most one extra probe per room. Process-local, like `core._room_locks`.
_probed: set[str] = set()


async def _drop_probe_session(target_url: str, room_key: str, config: Settings) -> None:
    """Best-effort: remove the warm-up probe's throwaway session from the room.

    Failure is logged and swallowed: the probe has already done its job by then,
    and the only cost of a session that outlives it is one stray turn a
    cross-session `session_search` could surface (see delete_hermes_session).

    Args:
        target_url: The room's Hermes container base URL.
        room_key: The room key core routes on (log context only).
        config: Application settings.
    """
    try:
        await delete_hermes_session(target_url, WARMUP_SESSION_ID, config.HERMES_API_SERVER_KEY)
    except httpx.HTTPError as exc:
        logger.warning(f"Could not delete warm-up session for room {room_key}: {exc}")


async def _probe_agent(target_url: str, room_key: str, config: Settings) -> bool:
    """Spend one throwaway turn on the room's agent, then delete its session.

    A running container is not yet a warm agent: the Hermes process pays a
    one-time tool-registry probe (~4.5 s of capability checks and vision
    auto-detection) on its *first* chat turn and memoizes the result
    process-wide, so whichever turn goes first eats it. This makes that turn a
    router-owned ping on WARMUP_SESSION_ID instead of the user's real question,
    and then deletes the session so nothing of it stays in the room's state.db.

    Args:
        target_url: The room's Hermes container base URL.
        room_key: The room key core routes on.
        config: Application settings.

    Returns:
        True when the probe turn completed (the room counts as probed), False
        when it failed — the caller then leaves the room unprobed so a later
        warm-up retries.
    """
    started = time.perf_counter()
    probed = True
    try:
        await ask_hermes_agent(
            target_url,
            WARMUP_SESSION_ID,
            WARMUP_PROMPT,
            config.HERMES_API_SERVER_KEY,
            idle_timeout_seconds=config.HERMES_IDLE_TIMEOUT_SECONDS,
            max_seconds=_WARMUP_MAX_SECONDS,
        )
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        # Same tuple as agent_turn._generate_handoff's: the probe is a
        # best-effort extra, and a failed one only costs the user's first turn
        # its cold start.
        logger.warning(
            f"Agent warm-up probe failed for room {room_key}; "
            f"first turn pays the cold start ({exc})"
        )
        probed = False
    finally:
        # Unconditional: a probe that failed mid-turn may still have made
        # Hermes create the session, and an unexpected exception type (which
        # propagates to _run_warmup's error log) must not leave one behind
        # either.
        await _drop_probe_session(target_url, room_key, config)
    if probed:
        logger.info(f"Agent warm for room {room_key} ({elapsed_ms(started)} ms)")
    return probed


async def _run_warmup(room_key: str, config: Settings) -> None:
    """Body of one warm-up: start the room's container, then warm its agent.

    Two steps, because a healthy container still answers its first real turn
    slowly: Hermes builds its tool definitions on the first chat turn of the
    process and memoizes them from then on, so the probe turn (see
    `_probe_agent`) moves that cost off the user's first message. The probe is
    skipped for a room this process already probed (`_probed`).

    Each step fails on its own terms. The broad catch around the container step
    mirrors `agent_turn.ask_agent`'s guard around the same call: docker's
    exception types cannot be imported here (container_manager is the only
    module allowed to), and whatever went wrong must end in a log line, never
    an unobserved task exception — the same reason the probe step has one. The
    user is not told: the next real message walks the normal path and surfaces
    AGENT_FAILURE_NOTICE if it still fails.

    Args:
        room_key: The room whose container to start.
        config: Application settings.
    """
    try:
        target_url = await asyncio.to_thread(get_or_create_container, room_key, config)
    except asyncio.CancelledError:
        # Shutdown (cancel_warmups). The worker thread cannot be interrupted,
        # so the docker call runs to completion on its own; the container it
        # produces is found, not recreated, by the next resolution.
        logger.info(f"Container warm-up for room {room_key} cancelled at shutdown")
        raise
    except Exception as exc:
        # Same guard as agent_turn.ask_agent's container step (its twin; keep
        # in sync).
        reason = describe_error("container", exc)
        logger.error(f"Container warm-up failed for room {room_key}: {reason}")
        return
    logger.info(f"Container warm for room {room_key}")

    if room_key in _probed:
        return
    try:
        probed = await _probe_agent(target_url, room_key, config)
    except asyncio.CancelledError:
        logger.info(f"Agent warm-up for room {room_key} cancelled at shutdown")
        raise
    except Exception as exc:
        # Anything _probe_agent does not treat as an expected probe failure.
        reason = describe_error("agent", exc)
        logger.error(f"Agent warm-up failed for room {room_key}: {reason}")
        return
    if probed:
        _probed.add(room_key)


def warm_room(room_key: str, config: Settings) -> None:
    """Start the room's container and warm its agent in the background.

    Returns immediately; the caller carries on without waiting. Deduplicated
    per room: one in-flight warm-up task per room (`_warmups`), and the agent
    probe inside it runs at most once per room per process (`_probed`). No extra log
    context needs binding: `asyncio.create_task` copies the current
    contextvars (process_inbound's `room_key`, the adapter's request fields)
    and `asyncio.to_thread` carries them into the worker thread, so every line
    the warm-up logs — including container_manager's own `container=` — is
    already tagged with the room.

    Args:
        room_key: The room whose container to start.
        config: Application settings.
    """
    # `done()` rather than membership: a finished task stays registered until
    # its pop callback runs on the next loop iteration, and a trigger landing
    # in that window must still get its retry.
    existing = _warmups.get(room_key)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_run_warmup(room_key, config), name=f"warmup:{room_key}")
    _warmups[room_key] = task
    task.add_done_callback(lambda _done: _warmups.pop(room_key, None))


def cancel_warmups() -> None:
    """Cancel every in-flight room warm-up (called at lifespan shutdown).

    Makes the abandoned warm-ups visible in the log instead of leaving them
    to the runner's silent teardown. The worker threads themselves finish on
    their own (see _run_warmup).
    """
    for task in list(_warmups.values()):
        task.cancel()
