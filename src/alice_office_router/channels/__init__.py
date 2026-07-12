"""Inbound message channels (LINE, local dev, future telegram/TUI/mobile).

Each channel adapter module (`line.py`, `local.py`, …) owns one way messages
enter and leave this router, and translates between its platform's wire format
and the channel-agnostic contracts in `base.py`. The shared processing
pipeline (`pipeline.py`) never imports any adapter. See
docs/channel-interface.md for the design and the add-a-channel checklist.
"""
