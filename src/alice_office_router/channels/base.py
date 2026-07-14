"""The channel-agnostic inbound model and adapter contract every channel shares.

The channel-free core (`core.process_inbound`) only ever sees an
`InboundMessage`: each adapter has already parsed its own wire format and
turned media/stickers/locations into placeholder text, so the model carries
just an identity plus plain text. `ChannelAdapter` is the structural contract
each adapter satisfies; `channels.enabled_adapters` builds the enabled ones and
`main.py` mounts each at `/webhooks/{name}` (see docs/channel-interface-design.md).
"""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict


class ChannelAdapter(Protocol):
    """Structural contract for a channel adapter (see design §4.2).

    An adapter fully owns one channel's wire format — signature verification,
    inbound parsing, media download, dedup, and reply delivery — and exposes
    just a name and a FastAPI router. Its only outbound seam into the rest of
    the app is `core.process_inbound`; the core never sees channel specifics.

    Attributes:
        name: Unique identifier; also the `/webhooks/{name}` path segment and
            (from Phase 3) the room_key prefix.
    """

    name: str

    def api_router(self) -> APIRouter:
        """Return the FastAPI router to mount at `/webhooks/{name}`.

        Returns:
            An APIRouter whose handlers verify, parse, dedup, and reply for
            this channel, funneling inbound messages into core.process_inbound.
        """
        ...


class InboundMessage(BaseModel):
    """The only inbound shape the channel-free core understands.

    Attributes:
        channel: Originating adapter name, e.g. "line".
        room_key: Globally unique room key the core routes on. In Phase 1 this
            is still the bare native room id; channel prefixing is Phase 3.
        text: Plain user text; media/sticker/location are already resolved to a
            placeholder by the adapter before this model is built.
    """

    model_config = ConfigDict(extra="ignore")

    channel: str
    room_key: str
    text: str
