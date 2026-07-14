"""Channel adapters, the channel-free inbound model, and the enabled registry.

`base` holds `InboundMessage` and the `ChannelAdapter` Protocol; each channel
package (e.g. `line`) provides one adapter. `enabled_adapters(config)` is the
single, static registry `main.py` iterates to mount every adapter — no dynamic
discovery or plugin system (see docs/channel-interface-design.md §4.2).
"""

from __future__ import annotations

from alice_office_router.channels.api import ApiChannelAdapter
from alice_office_router.channels.base import ChannelAdapter
from alice_office_router.channels.line.adapter import LineAdapter
from alice_office_router.config import Settings


def enabled_adapters(config: Settings) -> list[ChannelAdapter]:
    """Build the channel adapters enabled for this deployment.

    Args:
        config: Application settings. Optional channels are gated on it: the
            first-party API channel is included only when API_CHANNEL_TOKEN is
            set ("not enabled" == "not in the list", not a per-station flag).

    Returns:
        The enabled adapters in mount order. LINE is always enabled; the API
        channel is appended only when its bearer token is configured.
    """
    adapters: list[ChannelAdapter] = [LineAdapter()]
    if config.API_CHANNEL_TOKEN:
        adapters.append(ApiChannelAdapter())
    return adapters
