"""Turn the agent's `google-auth://request` marker into the speaker's own link.

The Google MCPs inside a room's container know neither their room id nor this
router's public URL, so they cannot build an authorization link themselves
(exactly the constraint that shaped `outbox://` in file_links.py —
docs/file-share-design.md §5). What they *can* do is say "this speaker has no
token" in a way the agent will paste verbatim, and let the router fill in the
rest on the way out. This module is that router half:

- Inside the container, gmail/drive `token_manager.get_access_token` raises an
  error carrying the fixed placeholder `google-auth://request`; the
  third-party calendar MCP's error text is not ours to change, so the same
  instruction is also a rule in both system prompts and in the runtime-env
  skill (docs/google-auth-per-member-plan.md §3.3 — the same two-layer
  approach as §9 of the file-share design).
- `publish_auth_links` runs on every agent reply (core._take_turn), right
  after `publish_file_links`, and swaps each marker for the authorization URL
  of *this turn's speaker* — the one identity rule of plan §3.1.

Unlike an `outbox://` token the marker is a fixed string, not a capability: it
carries no secret, and whoever triggers it only ever gets their own link, so
there is nothing here to keep out of the logs.

Issuing a link also parks the message that triggered it under
`data/<room_id>/router_state/pending_auth/<member_key>.json`, so that when the
member finishes authorizing, the router can re-run the question they actually
asked instead of making them type it again (plan §3.4). Step 4 of that plan
adds the resume half; `read_pending_auth` here is the seam it reads through.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path

import structlog
from pydantic import ValidationError

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings
from alice_office_router.google_oauth import auth_url_for
from alice_office_router.google_tokens import member_key_for

# Structured events an operator filters by field (room_key, member) —
# docs/logging-design.md §5.1 — as in file_links.
struct_logger = structlog.stdlib.get_logger(__name__)

# What the MCPs and the system prompts tell the agent to paste. Fixed, not
# random: the replacement depends only on who is speaking in which room.
AUTH_MARKER = "google-auth://request"

# Matched as a whole token, so `google-auth://requests` (or any longer run of
# marker characters) is left alone — same trailing-lookahead reason as
# file_links._MARKER_RE, where \b would not do the job.
#
# The optional `(?:[?#][^\s]*)?` also swallows anything the agent tacks on
# immediately after the marker with no separating whitespace — observed in
# practice as a fabricated `?scope=calendar&prompt=consent` suffix, imitating
# a real OAuth URL it has seen in training. Left unmatched, that suffix would
# survive substitution stuck directly onto the real link with no separator
# (`...&member=line_u_t10_old?scope=calendar&prompt=consent`), corrupting the
# `member` query value and turning a working link into a 400. Consuming it
# here means the whole fabricated tail is discarded along with the marker, so
# only the router's own URL reaches the user.
_MARKER_RE = re.compile(re.escape(AUTH_MARKER) + r"(?:[?#][^\s]*)?(?![A-Za-z0-9_-])")

# Shown in place of a marker when LINE would not tell us who spoke: a group
# member who never added the OA as a friend has no userId we can key a token
# file on, so there is no link to give them (plan §3.1).
AUTH_LINK_ANONYMOUS_NOTICE = (
    "LINE 沒有提供你的身分，我無法幫你連結 Google 帳號。請先把我加為好友，再在群組裡問一次。"
)

# Shown in place of a marker when this deployment has no Google integration at
# all (no PUBLIC_BASE_URL, or no Web OAuth credentials). The container cannot
# know that — it has no router config — so it emits the marker either way and
# the honest answer is given here, as in file_links' disabled notice.
AUTH_LINKS_DISABLED_NOTICE = "（這個部署沒有開通 Google 整合，請聯絡管理員）"

# How long a parked message stays re-runnable. Matches google_oauth's own
# _PENDING_TTL_SECONDS: both clocks start when the link is issued, so a state
# token that has expired can never find a pending message either.
PENDING_AUTH_TTL_SECONDS = 600.0


def _pending_payload(msg: InboundMessage) -> str:
    """Serialize one parked message with the timestamp its TTL is measured from.

    Args:
        msg: The inbound message that triggered the authorization link.

    Returns:
        The JSON text to persist.
    """
    return json.dumps({"ts": time.time(), "message": msg.model_dump()}, ensure_ascii=False)


def write_pending_auth(
    config: Settings, room_id: str, member_key: str, msg: InboundMessage
) -> None:
    """Park the message a member asked just before being sent to Google.

    Written temp-file-then-rename so the resume path can never read a
    half-written record, and overwriting: a member who triggers the marker
    twice is waiting on their latest question, not their first.

    The file stays mode 0600 (mkstemp's default). It lives inside the room's
    /opt/data mount but is router-only state — nothing in the container reads
    it, unlike a member token file, which must stay readable by the MCP
    subprocesses (google_tokens.save_member_tokens).

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).
        member_key: The member the authorization link was issued to.
        msg: The inbound message to re-run once they authorize.
    """
    path = config.room_pending_auth_path(room_id, member_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(_pending_payload(msg))
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _read_and_consume(path: Path, room_id: str, member_key: str) -> str | None:
    """Read a parked record and delete it, whatever its content turns out to be.

    Deleting up front is what makes the resume path single-shot: a record is
    re-run at most once, even if authorizing twice fires the hook twice.

    Args:
        path: The pending file for one member.
        room_id: Raw LINE room/user/group id (log context).
        member_key: The member whose record this is (log context).

    Returns:
        The file's text, or None when there is nothing parked (the normal
        case, so it is normalized here rather than raised) or it could not be
        read at all.
    """
    try:
        raw: str | None = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        struct_logger.error(
            "pending_auth_unreadable",
            room_key=room_id,
            member=member_key,
            error=type(exc).__name__,
        )
        raw = None
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        struct_logger.error(
            "pending_auth_cleanup_failed",
            room_key=room_id,
            member=member_key,
            error=type(exc).__name__,
        )
    return raw


def read_pending_auth(config: Settings, room_id: str, member_key: str) -> InboundMessage | None:
    """Take the message one member parked while authorizing, if it is still live.

    Always consumes: the record is deleted whether it is returned, expired, or
    unusable, so no caller needs a cleanup branch and no stale question can be
    re-run by a later authorization.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).
        member_key: The member who just finished authorizing.

    Returns:
        The parked InboundMessage, or None when nothing was parked, the record
        is malformed (logged as an error), or it is older than
        PENDING_AUTH_TTL_SECONDS (logged as info).
    """
    raw = _read_and_consume(config.room_pending_auth_path(room_id, member_key), room_id, member_key)
    if raw is None:
        return None
    try:
        record = json.loads(raw)
        parked_at = float(record["ts"])
        msg = InboundMessage.model_validate(record["message"])
    except (ValueError, TypeError, KeyError, ValidationError) as exc:
        struct_logger.error(
            "pending_auth_malformed",
            room_key=room_id,
            member=member_key,
            error=type(exc).__name__,
        )
        return None
    age = time.time() - parked_at
    if age > PENDING_AUTH_TTL_SECONDS:
        struct_logger.info(
            "pending_auth_expired", room_key=room_id, member=member_key, age_seconds=round(age)
        )
        return None
    return msg


def _substitute(text: str, replacement: str) -> str:
    """Replace every marker in a reply with one fixed string.

    Every occurrence gets the same replacement rather than the first one
    winning: the marker resolves per speaker, not per occurrence, so two
    markers in one reply are two copies of the same sentence — repetitive at
    worst, never a dangling placeholder.

    Args:
        text: The agent's reply text.
        replacement: The link (or notice) to substitute in.

    Returns:
        The text with every marker replaced. A lambda is used rather than a
        plain replacement string, so a backslash or group reference inside the
        replacement is never re-interpreted by `re`.
    """
    return _MARKER_RE.sub(lambda _match: replacement, text)


def _link_text(msg: InboundMessage, member_key: str, config: Settings) -> str:
    """Build the sentence that replaces the marker for an identified speaker.

    Args:
        msg: The inbound message being answered.
        member_key: The speaker's account key.
        config: Application settings.

    Returns:
        The user-facing sentence, addressed by display name in a group so a
        link broadcast to everyone still says whose it is (plan §3.3), and
        unaddressed in a 1:1 room where there is nobody else to confuse.
    """
    who = f"{msg.sender_name} " if msg.is_group and msg.sender_name else ""
    auth_url = auth_url_for(config, msg.room_key, member_key)
    return f"{who}請點此連結 Google 帳號（只會連結你自己的帳號）：\n{auth_url}"


async def publish_auth_links(text: str, msg: InboundMessage, config: Settings) -> tuple[str, bool]:
    """Rewrite an agent reply's authorization markers into this speaker's link.

    Called for every reply core is about to deliver, so — like
    publish_file_links — a reply with no marker (the overwhelming majority)
    does no I/O at all and comes back byte-identical.

    Args:
        text: The agent's reply text, after file links have been published.
        msg: The inbound message this reply answers; it decides whose link
            this is (google_tokens.member_key_for) and is parked for re-run.
        config: Application settings.

    Returns:
        Tuple of (text, requested): the rewritten reply, and whether an actual
        authorization link was issued — True only when a link went out, so
        core can mark the turn "auth_link". The two substituted notices (this
        deployment has no Google integration; LINE would not identify the
        speaker) are dead ends with nothing to resume, and report False.
    """
    if not _MARKER_RE.search(text):
        return text, False
    if not config.google_oauth_enabled:
        return _substitute(text, AUTH_LINKS_DISABLED_NOTICE), False

    member_key = member_key_for(msg)
    if member_key is None:
        return _substitute(text, AUTH_LINK_ANONYMOUS_NOTICE), False

    await asyncio.to_thread(write_pending_auth, config, msg.room_key, member_key, msg)
    struct_logger.info("auth_link_issued", room_key=msg.room_key, member=member_key)
    return _substitute(text, _link_text(msg, member_key, config)), True
