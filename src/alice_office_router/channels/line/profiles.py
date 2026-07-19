"""Group-member display-name lookup for LINE, with an in-memory TTL cache.

A group message event only carries the speaker's bare `userId`, but the group
prompt (and the observed buffer) want a human name. LINE exposes the member's
display name via `GET /v2/bot/group|room/{id}/member/{userId}` — no friending
required — which this module calls through line-bot-sdk's async API, mirroring
how `client.py` uses the SDK. Per CLAUDE.md's routing table this wire-format
knowledge stays inside `channels/line/`.

Every lookup is best-effort: a failed API call or a missing userId falls back
(design §8) and never raises, so name resolution can never block or drop a
message. Results are cached (15 min TTL, bounded) so a chatty group doesn't hit
the LINE API once per message.
"""

from __future__ import annotations

import logging
import time

from linebot.v3.messaging import AsyncApiClient, AsyncMessagingApi, Configuration
from linebot.v3.messaging.exceptions import ApiException

logger = logging.getLogger(__name__)

# Fallback name when even the userId is absent (design §8).
_FALLBACK_MEMBER = "成員"
# How many leading userId chars to show when the profile lookup can't resolve
# a real display name — enough to tell two anonymous speakers apart.
_ID_PREFIX_LEN = 8


class _ProfileCache:
    """Bounded, TTL'd in-memory cache of (room id, user id) -> display name.

    Mirrors dedup.EventDeduplicator's bounded map: entries carry an insertion
    timestamp, are considered stale past `ttl_seconds`, and the oldest ~10% are
    evicted once `max_size` is reached (so we trim in batches, not per insert).
    Process-local — fine for the current single-worker deployment (README).
    """

    def __init__(self, ttl_seconds: float = 900.0, max_size: int = 2048) -> None:
        """Initialize the cache.

        Args:
            ttl_seconds: How long a cached name stays fresh (default 15 min).
            max_size: Maximum entries retained before evicting the oldest ~10%.
        """
        self._entries: dict[tuple[str, str], tuple[str, float]] = {}
        self._ttl = ttl_seconds
        self._max_size = max_size

    def get(self, key: tuple[str, str]) -> str | None:
        """Return the cached display name for `key`, or None if absent/stale.

        Args:
            key: The (room id, user id) pair identifying a group member.

        Returns:
            The fresh cached name, or None when missing or past its TTL (a
            stale entry is dropped so it is re-fetched next time).
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        name, inserted = entry
        if time.time() - inserted > self._ttl:
            del self._entries[key]
            return None
        return name

    def put(self, key: tuple[str, str], name: str) -> None:
        """Cache `name` for `key`, evicting the oldest entries when full.

        Args:
            key: The (room id, user id) pair identifying a group member.
            name: The resolved display name to remember.
        """
        if key not in self._entries and len(self._entries) >= self._max_size:
            self._evict_oldest_tenth()
        self._entries[key] = (name, time.time())

    def _evict_oldest_tenth(self) -> None:
        """Drop the oldest ~10% of entries so we don't trim on every insert."""
        cutoff_index = max(len(self._entries) // 10, 1)
        cutoff = sorted(inserted for _, inserted in self._entries.values())[cutoff_index]
        self._entries = {k: v for k, v in self._entries.items() if v[1] > cutoff}


_CACHE = _ProfileCache()


async def _fetch_display_name(
    source_type: str | None, native_room_id: str, user_id: str, token: str
) -> str:
    """Fetch a group/room member's display name from the LINE Messaging API.

    Args:
        source_type: The event source type ("group" or "room"); selects the
            group vs. room member-profile endpoint.
        native_room_id: The bare LINE groupId/roomId (never a room_key).
        user_id: The bare LINE userId of the member to look up.
        token: LINE channel access token for authentication.

    Returns:
        The member's display name as reported by LINE.

    Raises:
        linebot.v3.messaging.exceptions.ApiException: If the LINE API rejects
            the request (e.g. the member has left the room).
    """
    configuration = Configuration(access_token=token)
    async with AsyncApiClient(configuration) as api_client:
        messaging_api = AsyncMessagingApi(api_client)
        if source_type == "room":
            profile = await messaging_api.get_room_member_profile(native_room_id, user_id)
        else:
            profile = await messaging_api.get_group_member_profile(native_room_id, user_id)
    return str(profile.display_name)


async def _lookup_display_name(
    source_type: str | None, native_room_id: str, user_id: str, token: str
) -> str | None:
    """Return a member's display name from cache or the LINE API, or None.

    Args:
        source_type: The event source type ("group" or "room").
        native_room_id: The bare LINE groupId/roomId.
        user_id: The bare LINE userId of the member to look up.
        token: LINE channel access token for authentication.

    Returns:
        The display name, or None when the lookup fails (logged, never raised —
        the caller applies the userId-prefix fallback). Both a LINE API
        rejection (ApiException, e.g. the member left) and a transient network
        error (the SDK's aiohttp client raises ClientError / TimeoutError, not
        ApiException) are absorbed, upholding the best-effort "never raises"
        contract so a profile-API outage can never 500 the webhook.
    """
    key = (native_room_id, user_id)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        name = await _fetch_display_name(source_type, native_room_id, user_id, token)
    except ApiException as exc:
        logger.info(f"Could not resolve LINE member {user_id} in {native_room_id}: {exc}")
        return None
    except Exception as exc:
        logger.warning(f"LINE member lookup failed for {user_id} in {native_room_id}: {exc}")
        return None
    _CACHE.put(key, name)
    return name


async def resolve_sender_name(
    source_type: str | None, native_room_id: str | None, user_id: str | None, token: str
) -> str:
    """Resolve a group speaker's display name, always returning something usable.

    Best-effort by contract (design §8): a lookup failure or a missing id falls
    back rather than raising, so it can never block message handling.

    Args:
        source_type: The event source type ("group" or "room").
        native_room_id: The bare LINE groupId/roomId, or None if unresolvable.
        user_id: The speaker's bare LINE userId, or None if LINE omitted it.
        token: LINE channel access token for authentication.

    Returns:
        The member's display name; the first 8 chars of the userId when the
        profile can't be fetched; or "成員" when there is no userId at all.
    """
    if not user_id:
        return _FALLBACK_MEMBER
    if native_room_id:
        name = await _lookup_display_name(source_type, native_room_id, user_id, token)
        if name:
            return name
    return user_id[:_ID_PREFIX_LEN]
