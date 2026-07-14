"""The LINE channel adapter and its wire-format helpers.

`adapter.LineAdapter` owns the LINE Messaging API wire format end to end:
signature verification (`verify`), webhook event parsing and media download
(`events`), reply/push delivery (`client`), Markdown stripping and bubble
chunking (`format`), and webhook-event dedup (`dedup`). Nothing here leaks
into the channel-free core, which only ever sees an `InboundMessage`.
"""

from __future__ import annotations
