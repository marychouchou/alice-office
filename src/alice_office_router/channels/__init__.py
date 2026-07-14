"""Channel adapters and the channel-free inbound model.

Phase 1 ships only `base.InboundMessage`; the `ChannelAdapter` Protocol,
`enabled_adapters(config)` registry, and the per-channel adapter packages
arrive in later phases (see docs/channel-interface-plan.md).
"""

from __future__ import annotations
