"""Turn the agent's `outbox://<token>` markers into real download links.

LINE's Messaging API has no outbound file message type at all (see
docs/file-share-design.md §1), so the only way to hand a user the summary.md or
report.xlsx their agent produced is to host it and send a link. This module is
the router half of that handoff:

- Inside the container, the `share_file` plugin tool copies the file to
  `$HERMES_HOME/outbox/<token>/<name>` (= `data/<room_id>/outbox/…` here) and
  returns the placeholder `outbox://<token>` for the agent to paste into its
  reply. The container never learns its room id or this router's public URL,
  so no new container env var — and therefore no `docker rm -f` for existing
  rooms (docs/file-share-design.md §5).
- `publish_file_links` runs on every agent reply (core._take_turn) and swaps
  each marker for `{PUBLIC_BASE_URL}/files/{room_id}/{token}`.

The router copies the file **out of** the room's mount before serving it, and
`files_router` reads only from that copy. The room's own agent owns everything
under `data/<room_id>/`, so anything still in there could be a symlink into
another room, could have had its mtime pushed forward to extend the link's
life, or could have been written past the size cap by a shell instead of the
tool (docs/file-share-design.md §4). One copy, validated at copy time with
`O_NOFOLLOW` + `fstat`, removes all three: from then on the served bytes, the
size and the TTL clock are the router's own.

The link is a capability URL — 256 bits of unguessable token plus a TTL,
whoever holds it can download it. That is deliberately the same security level
as a file sent natively into a LINE room, which any member can forward on
(design §2 and §7). Tokens are therefore keys: never log one whole.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from alice_office_router.config import Settings, get_settings

# Every line this module logs is a structured event an operator filters by
# field (room_key, reason) — docs/logging-design.md §5.1 — so there is no
# stdlib free-text logger here.
struct_logger = structlog.stdlib.get_logger(__name__)

# What the plugin tool pastes into the reply. 43 characters is exactly what
# secrets.token_urlsafe(32) produces; the trailing lookahead (rather than \b)
# rejects a longer run of token characters, which \b would not for a token
# ending in "-" (both "-" and a following space are non-word characters, so
# there is no boundary between them).
_MARKER_RE = re.compile(r"outbox://([A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])")

# Same room-key shapes the API channel accepts — see channels/api.py's
# _ROOM_KEY_RE, whose comment explains the two shapes and carries the pointer
# back here. Copied rather than shared: second occurrence, so the Rule of
# Three says duplicate with cross-references and extract on the third.
_ROOM_ID_RE = re.compile(r"(?:line_[UCR][0-9a-f]{32}|api_[a-z0-9-]{1,32})")

# The token shape as it appears in a URL path segment.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")

# How much of a token may appear in a log line. The token IS the download
# credential, so a full one in the log stream would hand every reader of that
# stream the file (docs/file-share-design.md §7); eight characters is enough to
# tie a warning to the reply that produced it.
_TOKEN_LOG_CHARS = 8

# Characters a published filename must never carry: control characters (a CR
# or LF would reach a Content-Disposition header), quotes and backslashes
# (which that header quotes with), and a path separator. The name comes off
# the agent's own filesystem, so the plugin's sanitizing is not the fence —
# this is.
_UNSAFE_NAME_RE = re.compile(r'[\x00-\x1f\x7f"\\/]')

_SECONDS_PER_HOUR = 3600

# Shown in place of a marker when this deployment never set PUBLIC_BASE_URL.
# The container cannot know whether the router has file links configured (it
# has no router config at all), so `share_file` always succeeds and the honest
# answer is given here, in the user's own language, instead of leaking a
# placeholder string into the chat window.
FILE_LINKS_DISABLED_NOTICE = "（此部署未設定檔案下載連結，請聯絡管理員）"

# Shown in place of a marker whose file could not be published: no such token,
# more than one entry in it, a symlink, or over FILE_LINK_MAX_BYTES.
FILE_LINK_INVALID_NOTICE = "（檔案連結無效或已過期，請再請我分享一次）"

files_router = APIRouter()


class _Rejected(Exception):
    """One outbox entry failed validation and must not be published.

    Attributes:
        reason: Short, content-free reason for the log line — never the
            filename, the file's content, or the full token.
    """

    def __init__(self, reason: str) -> None:
        """Record why the entry was rejected.

        Args:
            reason: Short, content-free reason string.
        """
        super().__init__(reason)
        self.reason = reason


def _safe_name(name: str) -> str:
    """Normalize an agent-chosen filename into one safe to publish and serve.

    Args:
        name: The single path component found in the outbox token directory.

    Returns:
        The name with unsafe characters replaced by underscores and any
        leading dots removed (so a published file is never hidden from
        `_published_file`'s dotfile filter), or "file" when nothing is left.
    """
    cleaned = _UNSAFE_NAME_RE.sub("_", name).strip().lstrip(".")
    return cleaned or "file"


def _published_file(token_dir: Path) -> Path | None:
    """Return the one published file under a token directory, if there is one.

    Args:
        token_dir: DATA_DIR / "_files" / room_id / token.

    Returns:
        The single regular, non-symlink file in that directory, or None when
        the directory does not exist, is unreadable, or does not hold exactly
        one such file. A missing directory is the normal "nothing published
        under this token" case, so it is normalized to None here rather than
        raising — callers then have one code path, not two.
    """
    try:
        # Dot-prefixed names are this module's own in-progress copies
        # (_copy_to_published), never a published file: _safe_name strips
        # leading dots, so a leftover .part from a failed copy can't
        # permanently poison the "exactly one entry" check below.
        entries = [entry for entry in token_dir.iterdir() if not entry.name.startswith(".")]
    except OSError:
        return None
    if len(entries) != 1:
        return None
    entry = entries[0]
    if entry.is_symlink() or not entry.is_file():
        return None
    return entry


def _open_outbox_entry(token_dir: Path) -> tuple[str, int]:
    """Open the single file the agent left under one outbox token directory.

    Both the directory and the file are opened with O_NOFOLLOW, and the file
    is opened relative to the directory's own descriptor, so neither can be
    swapped for a symlink pointing at another room's data between the checks
    and the copy.

    Args:
        token_dir: DATA_DIR / room_id / "outbox" / token.

    Returns:
        Tuple of (filename, open read-only file descriptor). The caller owns
        the descriptor and must close it.

    Raises:
        _Rejected: If the directory is missing, is a symlink, is unreadable,
            holds anything other than exactly one entry, or that entry cannot
            be opened without following a symlink.
    """
    try:
        dir_fd = os.open(token_dir, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError as exc:
        raise _Rejected("outbox_dir_unusable") from exc
    try:
        # noqa PTH208: pathlib cannot list a directory by descriptor, and the
        # descriptor is the point — it pins the directory we just opened with
        # O_NOFOLLOW, so the entry is read from that exact inode.
        names = os.listdir(dir_fd)  # noqa: PTH208
        if len(names) != 1:
            raise _Rejected(f"outbox_entry_count={len(names)}")
        return names[0], os.open(names[0], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise _Rejected("outbox_entry_unusable") from exc
    finally:
        os.close(dir_fd)


def _copy_to_published(fd: int, name: str, dest_dir: Path, config: Settings) -> Path:
    """Copy one opened outbox file into the router-owned publish directory.

    The size and file-type checks read the already-open descriptor, so they
    describe the bytes actually copied rather than whatever the path pointed
    at a moment earlier.

    Args:
        fd: Read-only descriptor from _open_outbox_entry.
        name: The entry's filename, sanitized here before use.
        dest_dir: DATA_DIR / "_files" / room_id / token.
        config: Application settings (for FILE_LINK_MAX_BYTES).

    Returns:
        Path to the published copy.

    Raises:
        _Rejected: If the descriptor is not a regular file, the file exceeds
            FILE_LINK_MAX_BYTES, or the copy itself fails.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _Rejected("not_a_regular_file")
    if info.st_size > config.FILE_LINK_MAX_BYTES:
        raise _Rejected(f"too_large_bytes={info.st_size}")
    dest = dest_dir / _safe_name(name)
    partial = dest.with_name(f".{dest.name}.part")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(fd, "rb", closefd=False) as source, partial.open("wb") as target:
            shutil.copyfileobj(source, target)
        partial.replace(dest)
    except OSError as exc:
        raise _Rejected("copy_failed") from exc
    return dest


def _discard_outbox(token_dir: Path, room_id: str, token: str) -> None:
    """Best-effort: remove the agent's handoff copy once it has been published.

    Failure only leaves a stale directory inside the room's own data dir; the
    link already works, so it is logged rather than propagated.

    Args:
        token_dir: DATA_DIR / room_id / "outbox" / token.
        room_id: The room whose outbox this is (log context).
        token: The token being published (logged truncated).
    """
    try:
        shutil.rmtree(token_dir)
    except OSError as exc:
        struct_logger.warning(
            "file_link_outbox_cleanup_failed",
            room_key=room_id,
            token_prefix=token[:_TOKEN_LOG_CHARS],
            error=type(exc).__name__,
        )


def _sweep_expired(room_id: str, config: Settings) -> None:
    """Delete this room's published files whose TTL has run out.

    Runs on every publish for the room, which is why there is no scheduler and
    no "is cleanup due" branch. The sweep keys on each token directory's own
    mtime (written when the published copy was renamed into it); the download
    route independently re-checks the served file's mtime, so a directory this
    sweep has not reached yet is still not downloadable.

    Args:
        room_id: The room whose published directory to sweep.
        config: Application settings (for FILE_LINK_TTL_HOURS).
    """
    room_dir = config.room_published_dir(room_id)
    cutoff = time.time() - config.FILE_LINK_TTL_HOURS * _SECONDS_PER_HOUR
    try:
        entries = list(room_dir.iterdir())
    except OSError as exc:
        struct_logger.warning("file_link_sweep_failed", room_key=room_id, error=type(exc).__name__)
        return
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
        except OSError as exc:
            struct_logger.warning(
                "file_link_sweep_failed", room_key=room_id, error=type(exc).__name__
            )


def ensure_published(room_id: str, token: str, config: Settings) -> Path | None:
    """Publish one token's file out of the room's outbox, if it isn't already.

    Synchronous and I/O-bound; `publish_file_links` calls it off the event
    loop. Idempotent by design: an agent that pastes the same link again in a
    later turn finds the published copy and keeps the original TTL, rather
    than re-copying from an outbox directory this call already deleted.

    Args:
        room_id: The room the marker came from, in its original case.
        token: The 43-character token from the marker.
        config: Application settings.

    Returns:
        Path to the published copy under room_published_dir, or None when the
        outbox entry failed validation (a warning is logged with a truncated
        token and a content-free reason).
    """
    dest_dir = config.room_published_dir(room_id) / token
    already = _published_file(dest_dir)
    if already is not None:
        return already

    outbox_dir = config.room_outbox_dir(room_id) / token
    try:
        name, fd = _open_outbox_entry(outbox_dir)
        try:
            published = _copy_to_published(fd, name, dest_dir, config)
        finally:
            os.close(fd)
    except _Rejected as exc:
        struct_logger.warning(
            "file_link_rejected",
            room_key=room_id,
            token_prefix=token[:_TOKEN_LOG_CHARS],
            reason=exc.reason,
        )
        return None

    _discard_outbox(outbox_dir, room_id, token)
    _sweep_expired(room_id, config)
    return published


def _substitute(text: str, resolve: Callable[[str], str | None]) -> str:
    """Replace every outbox marker in a text using a token resolver.

    Args:
        text: The agent's reply text.
        resolve: Maps a token to its replacement, or None when the token has
            no usable file (the fixed invalid notice is used instead).

    Returns:
        The text with every marker replaced. Text without markers is returned
        unchanged, so this is safe to run over every reply.
    """

    def _replacement(match: re.Match[str]) -> str:
        return resolve(match.group(1)) or FILE_LINK_INVALID_NOTICE

    return _MARKER_RE.sub(_replacement, text)


async def publish_file_links(text: str, room_id: str, config: Settings) -> str:
    """Rewrite an agent reply's outbox markers into user-facing download URLs.

    Called for every reply core is about to deliver, so it must be cheap and
    total: a reply with no marker (the overwhelming majority) does no I/O and
    comes back byte-identical.

    Args:
        text: The agent's reply text, exactly as the agent wrote it.
        room_id: The room key core routes on, which is also this room's
            directory name under DATA_DIR.
        config: Application settings.

    Returns:
        The reply text with each marker replaced by a download URL, by the
        fixed "not configured" notice when this deployment has no
        PUBLIC_BASE_URL, or by the fixed "invalid or expired" notice when the
        token's file could not be published.
    """
    if not config.file_links_enabled:
        return _substitute(text, lambda _token: FILE_LINKS_DISABLED_NOTICE)

    tokens: list[str] = [match.group(1) for match in _MARKER_RE.finditer(text)]
    if not tokens:
        return text

    links: dict[str, str] = {}
    for token in dict.fromkeys(tokens):
        path = await asyncio.to_thread(ensure_published, room_id, token, config)
        if path is not None:
            links[token] = f"{config.PUBLIC_BASE_URL}/files/{room_id}/{token}"
    if links:
        struct_logger.info("file_link_published", room_key=room_id, count=len(links))
    return _substitute(text, links.get)


def _resolve_download(room_id: str, token: str, config: Settings) -> Path | None:
    """Run the whole download validation chain for one request.

    Every failure looks the same to the caller on purpose: distinguishing
    "malformed token" from "expired" from "never existed" would turn the route
    into a token oracle (docs/file-share-design.md §7).

    Args:
        room_id: Raw room id from the URL path.
        token: Raw token from the URL path.
        config: Application settings.

    Returns:
        The resolved path to serve, or None when anything at all was wrong.
    """
    if not _ROOM_ID_RE.fullmatch(room_id) or not _TOKEN_RE.fullmatch(token):
        return None
    path = _published_file(config.room_published_dir(room_id) / token)
    if path is None:
        return None
    resolved = path.resolve()
    # Both sides resolved: on macOS host mode DATA_DIR often sits under
    # /var -> /private/var, so comparing a resolved file against an
    # unresolved root would reject every legitimate download.
    if not resolved.is_relative_to(config.published_files_dir.resolve()):
        return None
    if _is_expired(resolved, config):
        return None
    return resolved


def _is_expired(path: Path, config: Settings) -> bool:
    """Whether a published file is past its TTL.

    Args:
        path: The published copy (never the agent's outbox entry — its mtime
            is agent-controlled).
        config: Application settings (for FILE_LINK_TTL_HOURS).

    Returns:
        True when the file is older than the TTL, or when its mtime cannot be
        read at all — an unreadable file is not servable either way.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True
    return time.time() - mtime > config.FILE_LINK_TTL_HOURS * _SECONDS_PER_HOUR


# HEAD alongside GET: download managers and link-preview fetchers often probe
# size and filename before fetching, and FastAPI does not add HEAD to a GET
# route on its own. FileResponse already answers HEAD with headers only.
@files_router.api_route("/files/{room_id}/{token}", methods=["GET", "HEAD"])
async def download_file(
    room_id: str,
    token: str,
    config: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    """Serve one published file to whoever holds its link.

    Level 1 of the access model: the token is the credential and the router
    does not ask who is downloading (docs/file-share-design.md §2). Level 2
    (verify the caller is a member of `room_id` via a LIFF ID token) inserts
    here as a `Depends` on this route — the token layout, the `_files/`
    directory structure, and the URL shape are all already what it needs, so
    nothing below changes when it lands.

    Args:
        room_id: The room the file was published for (also in the URL so
            level 2 can check membership against it).
        token: The 43-character capability token.
        config: Application settings via dependency injection.

    Returns:
        A FileResponse, always as an attachment and always with nosniff — this
        origin also serves the OAuth routes, so a file the agent produced must
        never be rendered as a document here.

    Raises:
        HTTPException: 404 for a malformed room id or token, an unknown or
            expired token, or anything under `_files/` that is not exactly one
            regular file — all with the same detail, so the response cannot be
            used to enumerate tokens.
    """
    path = _resolve_download(room_id, token, config)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        path,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        filename=path.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )
