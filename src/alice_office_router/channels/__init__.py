"""Channel adapters, the channel-free inbound model, and the enabled registry.

`base` holds `InboundMessage` and the `ChannelAdapter` Protocol; each channel
package (e.g. `line`) provides one adapter. `enabled_adapters(config)` is the
single, static registry `main.py` iterates to mount every adapter — no dynamic
discovery or plugin system (see docs/channel-interface-design.md §4.2).
"""

from __future__ import annotations

from alice_office_router.channels.base import ChannelAdapter
from alice_office_router.channels.line.adapter import LineAdapter
from alice_office_router.config import Settings


def enabled_adapters(config: Settings) -> list[ChannelAdapter]:
    """Build the channel adapters enabled for this deployment.

    Args:
        config: Application settings (later phases gate optional channels on
            it, e.g. the first-party API channel on API_CHANNEL_TOKEN).

    Returns:
        The enabled adapters in mount order. LINE is always enabled.
    """
    return [LineAdapter()]
