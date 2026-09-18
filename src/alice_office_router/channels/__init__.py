"""Channel adapters, the channel-free inbound model, and the enabled registry.

`base` holds `InboundMessage` and the `ChannelAdapter` Protocol; each channel
package (e.g. `line`) provides one adapter. `enabled_adapters(config)` is the
single, static registry `main.py` iterates to mount every adapter — no dynamic
discovery or plugin system (see docs/channel-interface-design.md §4.2).

`register_adapters` / `adapter_for` are the *runtime* half of that: the mounted
adapters, keyed by name, so code that holds only an `InboundMessage` can find
the channel it came from. Exactly one caller needs this — `core.resume_pending_auth`,
which re-runs a parked message after Google authorization and has nothing but
`msg.channel` to route on.
"""

from __future__ import annotations

from collections.abc import Sequence

from alice_office_router.channels.api import ApiChannelAdapter
from alice_office_router.channels.base import ChannelAdapter
from alice_office_router.channels.line.adapter import LineAdapter
from alice_office_router.config import Settings

# The adapters this process mounted, by `adapter.name` — the same key an
# InboundMessage carries in `channel`. Process-local and populated once at app
# build (main.py), like core's `_room_locks`: no cross-process sharing, and a
# process that never built an app (a unit test importing core alone) simply
# finds nothing here, which `adapter_for` reports as None rather than raising.
_adapters: dict[str, ChannelAdapter] = {}


def register_adapters(adapters: Sequence[ChannelAdapter]) -> None:
    """Make these adapters the ones `adapter_for` resolves to.

    Replaces the registry wholesale rather than adding to it, so building a
    second app (a test's isolated FastAPI instance) can never leave another
    app's adapter behind to receive a resumed message.

    Args:
        adapters: The adapters this process mounted, in any order. Two
            adapters with the same `name` cannot both be mounted (they would
            share a `/webhooks/{name}` prefix), so the last one wins.
    """
    _adapters.clear()
    _adapters.update({adapter.name: adapter for adapter in adapters})


def adapter_for(channel: str) -> ChannelAdapter | None:
    """Find the mounted adapter for one channel name.

    Args:
        channel: A channel name as `InboundMessage.channel` carries it, e.g.
            "line".

    Returns:
        The registered adapter, or None when this process mounted no adapter
        under that name — a message parked by a channel that is no longer
        enabled, which the caller logs rather than treats as impossible.
    """
    return _adapters.get(channel)


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
