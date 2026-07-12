"""Channel-agnostic contracts between channel adapters and the pipeline.

A channel adapter turns whatever its platform delivers (a LINE webhook event,
a local HTTP request, a future telegram update) into an `InboundMessage` plus
a `Responder` bound to the triggering room, then hands both to
`pipeline.process_inbound`. Nothing in this module may import a specific
channel or any platform SDK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

# room_id doubles as the Docker container name suffix (hermes_<room_id>) and
# the data/<room_id>/ directory name, so it must stay within Docker's name
# charset and be path-traversal-safe. LINE ids ([UCR] + 32 hex = 33 chars)
# fit; channels whose native ids don't fit must normalize + prefix them
# (see docs/channel-interface.md「room_id 契約」).
_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class InboundMessage:
    """One user message, already resolved to text, entering the pipeline.

    Attributes:
        channel: Adapter name that produced this message (e.g. "line", "local").
        room_id: Globally-unique chatroom id; keys the container, data dir,
            and Hermes session. Must satisfy `is_safe_room_id`.
        text: Final text to forward to the room's Hermes agent — media has
            already been downloaded and replaced with a notice by the adapter.
    """

    channel: str
    room_id: str
    text: str


class Responder(Protocol):
    """Delivery half of a channel, bound to one triggering message's room.

    Implementations own every platform-specific delivery concern: formatting
    (e.g. Markdown stripping), message-length chunking, and delivery-mode
    choice (e.g. LINE reply token vs Push). Methods may raise; the pipeline
    logs and never retries.
    """

    async def send_reply(self, text: str) -> None:
        """Deliver the answer to the triggering message."""
        ...

    async def send_notice(self, text: str) -> None:
        """Deliver an additional side message to the same room.

        Used for out-of-band notices (e.g. a Google OAuth link) that must not
        consume the channel's answer-to-this-message mechanism, so a later
        `send_reply` in the same processing run still works.
        """
        ...


def is_safe_room_id(room_id: str) -> bool:
    """Check that a room id is safe to use as container name and path segment.

    Args:
        room_id: Candidate room id from a channel adapter.

    Returns:
        True if it matches `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` (Docker-name-safe,
        no path separators or dots, max 64 chars).
    """
    return _ROOM_ID_RE.fullmatch(room_id) is not None
