from __future__ import annotations

import time


class EventDeduplicator:
    """Bounded in-memory dedup for LINE webhook event IDs.

    LINE's webhook delivery is at-least-once — the platform may redeliver the
    same event on retry (e.g. if our 200 OK was slow or dropped). This keeps a
    bounded set of recently seen `webhookEventId`s so a redelivered event isn't
    processed (and answered) twice.

    State is in-process only, so it is not shared across multiple router
    workers/replicas — acceptable for the current single-process deployment
    (see README "部署模式").
    """

    def __init__(self, max_size: int = 1000) -> None:
        """Initialize the deduplicator.

        Args:
            max_size: Maximum number of event IDs to retain before evicting
                the oldest entries.
        """
        self._seen: dict[str, float] = {}
        self._max_size = max_size

    def is_duplicate(self, event_id: str) -> bool:
        """Check whether `event_id` was already seen, recording it either way.

        Args:
            event_id: The LINE `webhookEventId` for a single event. An empty
                string is never treated as a duplicate (and is not recorded).

        Returns:
            True if this event_id was already seen and should be skipped,
            False if it's new (or blank).
        """
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        if len(self._seen) >= self._max_size:
            self._evict_oldest_tenth()
        self._seen[event_id] = time.time()
        return False

    def _evict_oldest_tenth(self) -> None:
        """Drop the oldest ~10% of entries so we don't trim on every insert."""
        cutoff_index = max(len(self._seen) // 10, 1)
        cutoff = sorted(self._seen.values())[cutoff_index]
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
