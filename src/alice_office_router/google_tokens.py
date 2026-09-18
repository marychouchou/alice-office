"""Per-member Google token files for a room, and the tokens.json symlink swap.

A room's Google MCPs (google-calendar / gmail / drive) all read one token
file, whose path their write-once config.yaml pinned at
``/opt/google-workspace/tokens.json``. That single path has to serve every
member of a group chat, so the file itself becomes a relative symlink the
router repoints at whoever is speaking, between turns::

    data/<room_id>/google/
      gcp-oauth.keys.json            (seeded once, untouched here)
      gcp-oauth.keys.installed.json  (seeded once, untouched here)
      tokens.json -> members/<member_key>.json      <- the swap target
      members/
        <member_key>.json            {"<account_key(room_id)>": {...}}

Three verified facts about those MCPs shape this design
(docs/google-auth-per-member-plan.md §2):

1. Every tool call re-reads the token file from disk — only the OAuth2Client
   object is cached, and it is re-credentialed each call. So swapping the
   file between turns is enough: no container or MCP restart.
2. gmail/drive look the account up strictly by ``GOOGLE_ACCOUNT_MODE``, which
   each room's write-once config.yaml pinned to ``account_key(room_id)``, and
   google-calendar treats every key in the file as an available account. The
   *inner* key of a member file therefore stays ``account_key(room_id)`` —
   room-shaped, never member-shaped — and a member file holds exactly one key.
3. A token refresh inside an MCP rewrites the whole file through the symlink,
   and google-calendar unlinks it outright when it fails to parse. Member
   files are therefore written temp-file-then-rename, never in place.

``/opt/google-workspace`` is a whole-directory rw bind mount, so the symlink
resolves inside the container as long as its target is *relative*
(``members/<key>.json``) — an absolute host path would dangle.

The swap itself must only ever happen between turns, while the room's turn
lock is held (see core._take_turn); repointing it mid-turn would hand a
running tool call somebody else's account.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: importing channels.base at runtime pulls in channels/__init__
    # -> channels.api -> core -> auth_links -> google_oauth -> back here.
    from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings
from alice_office_router.room_seed import ensure_google_seed

logger = logging.getLogger(__name__)

# Scopes a member's token must fully carry before the gate stops nagging;
# a subset of google_oauth.SCOPES (calendar.events is implied by calendar
# for gate purposes). Lives here, next to its only reader.
REQUIRED_SCOPES = {
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
}

# Symlink target used for a speaker the channel could not identify (a group
# member who never added the OA as a friend, so LINE withholds their userId).
# Deliberately a file that is never written: the MCPs then report "no token"
# exactly as they do for an unauthorized member, so there is no third code
# path for "unknown speaker".
ANONYMOUS_MEMBER = "_anonymous"

# Seconds of remaining lifetime below which an access token counts as expired
# (in milliseconds, matching tokens.json's expiry_date unit).
_EXPIRY_SKEW_MS = 300_000


def account_key(room_id: str) -> str:
    """Normalize a LINE id into the Google account key used everywhere.

    @cocal/google-calendar-mcp validates GOOGLE_ACCOUNT_MODE against
    /^[a-z0-9_-]{1,64}$/, which rejects LINE's uppercase-prefixed ids
    (U.../C.../R...) outright. Lowercasing is therefore mandatory, not
    cosmetic — member filenames, the key inside each member file, the oauth
    routes, and the gate must all agree.

    Args:
        room_id: Raw LINE room/user/group id (also used for a sender id,
            which is the same shape).

    Returns:
        The lowercased id, used as the Google account key.
    """
    return room_id.lower()


def member_key_for(msg: InboundMessage) -> str | None:
    """Decide whose Google account this turn should run as.

    The single identity rule (plan §3.1): in a group, every turn runs as the
    person who spoke; in a 1:1 room the member *is* the room, so both cases
    take the same swap path and a 1:1 room simply swaps to the same file
    every time.

    Args:
        msg: The inbound message about to be handled.

    Returns:
        The speaker's account key, or None when this is a group message whose
        sender LINE would not identify (they have not added the OA as a
        friend) — such a turn gets ANONYMOUS_MEMBER and no Google access.
    """
    if not msg.is_group:
        return account_key(msg.room_key)
    if msg.sender_id:
        return account_key(msg.sender_id)
    return None


def load_member_tokens(
    config: Settings, room_id: str, member_key: str
) -> dict[str, dict[str, object]]:
    """Load one member's token file, tolerating a missing file.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).
        member_key: The member's account key (see member_key_for).

    Returns:
        Mapping of account key to that account's token data — in practice a
        single entry keyed by account_key(room_id). A member who has never
        authorized has no file, and normalizes to an empty dict here so no
        caller needs a "not authorized yet" branch.

    Raises:
        OSError: If the file exists but cannot be read.
        ValueError: If the file exists but is not valid JSON.
    """
    path = config.room_google_member_tokens_path(room_id, member_key)
    if not path.exists():
        return {}
    tokens: dict[str, dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
    return tokens


def save_member_tokens(
    config: Settings, room_id: str, member_key: str, tokens: dict[str, dict[str, object]]
) -> None:
    """Write one member's token file so it is never observable half-written.

    A Google MCP can be reading this exact file (through the tokens.json
    symlink) while the OAuth callback writes it, so the content is staged in
    a temp file in the same directory and renamed over the destination.

    The result is mode 0644, not mkstemp's default 0600: the room container's
    MCP subprocesses run as uid 10000, not root. Same trick, same reason, as
    room_seed._copy_atomically — see there for the other half of this pair.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).
        member_key: The member's account key (see member_key_for).
        tokens: Full account key -> token data mapping to persist.
    """
    path = config.room_google_member_tokens_path(room_id, member_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(tokens, tmp, indent=2)
            os.fchmod(tmp.fileno(), 0o644)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def migrate_legacy_tokens(config: Settings, room_id: str) -> None:
    """Move a pre-member-store tokens.json into the members directory, once.

    Before this design a room had exactly one token file and it *was*
    tokens.json. One rule covers both room kinds: that file becomes
    ``members/<account_key(room_id)>.json``. For a 1:1 room that is precisely
    the member key its own messages produce, so its authorization carries
    over untouched; for a group room nobody will ever select that key again,
    which is the intended outcome — the shared group token is retired and
    each member authorizes for themselves.

    Idempotent: a tokens.json that is already a symlink (or absent) is left
    alone, so this is safe to call on every turn.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).
    """
    legacy_path = config.room_google_tokens_path(room_id)
    if legacy_path.is_symlink() or not legacy_path.is_file():
        return
    dest = config.room_google_member_tokens_path(room_id, account_key(room_id))
    dest.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.replace(dest)
    logger.info(f"Migrated legacy tokens.json of room [{room_id}] to {dest}")


def select_member_tokens(config: Settings, room_id: str, member_key: str | None) -> bool:
    """Point this room's tokens.json at one member's own token file.

    Creates or replaces ``data/<room_id>/google/tokens.json`` as a *relative*
    symlink to ``members/<member_key>.json`` (see module docstring for why
    relative, and why a missing target is a fine outcome rather than an
    error). The replacement goes through a temp symlink plus os.replace, so a
    Google MCP reading the path concurrently sees one target or the other,
    never a gap.

    Must be called only between turns, with the room's turn lock held.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).
        member_key: The speaker's account key, or None for an unidentified
            group speaker — which points at ANONYMOUS_MEMBER, a file that is
            never written.

    Returns:
        True only when the link actually moved (a different member now, or no
        link at all before). False for the two no-ops: Google is disabled for
        this deployment, or the link already names this member — the 1:1
        steady state. Callers use it to run the after-swap work exactly on the
        turns that need it (core._take_turn nudges the container's mount).
    """
    if not config.google_oauth_enabled:
        return False
    # A room can reach its first turn before its container (and thus its
    # google/ dir) exists; ensure_google_seed is the idempotent way to get
    # the directory *and* this room's credential copies in place.
    ensure_google_seed(room_id, config)
    migrate_legacy_tokens(config, room_id)

    target = Path("members") / f"{member_key or ANONYMOUS_MEMBER}.json"
    tokens_path = config.room_google_tokens_path(room_id)
    # readlink, not resolve(): the comparison is about the link itself, and
    # the target usually does not exist yet.
    if tokens_path.is_symlink() and tokens_path.readlink() == target:
        return False

    tmp_path = tokens_path.with_name(f".{tokens_path.name}.{secrets.token_hex(8)}")
    tmp_path.symlink_to(target)
    try:
        tmp_path.replace(tokens_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info(f"Room [{room_id}] Google tokens.json now points at {target}")
    return True


def check_member_token(config: Settings, room_id: str, member_key: str | None) -> str:
    """Classify one member's Google token status in a room.

    Args:
        config: Application settings.
        room_id: Raw LINE room/user/group id (original case).
        member_key: The member's account key, or None for an unidentified
            group speaker (who can never have a token).

    Returns:
        "missing" (no usable token), "missing_scopes" (token present but
        REQUIRED_SCOPES not fully granted), or "ok".
    """
    if member_key is None:
        return "missing"
    try:
        tokens = load_member_tokens(config, room_id, member_key)
        token_data = tokens.get(account_key(room_id))
        if token_data is None:
            return "missing"
        expiry = float(token_data.get("expiry_date", 0))  # type: ignore[arg-type]
        if time.time() * 1000 >= expiry - _EXPIRY_SKEW_MS and not token_data.get("refresh_token"):
            return "missing"
        granted = set(str(token_data.get("scope", "")).split())
        if not REQUIRED_SCOPES.issubset(granted):
            return "missing_scopes"
        return "ok"
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        logger.error(
            f"Failed to read Google tokens for room [{room_id}] member [{member_key}]: {exc}"
        )
        return "missing"
