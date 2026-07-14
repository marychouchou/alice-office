"""The channel-agnostic inbound message model shared by every adapter.

The channel-free core (`core.process_inbound`) only ever sees an
`InboundMessage`: each adapter has already parsed its own wire format and
turned media/stickers/locations into placeholder text, so the model carries
just an identity plus plain text. The `ChannelAdapter` Protocol is deferred to
Phase 2 (see docs/channel-interface-plan.md).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


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
