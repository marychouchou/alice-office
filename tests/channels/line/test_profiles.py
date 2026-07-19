from __future__ import annotations

import time
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.channels.line import profiles
from alice_office_router.channels.line.profiles import _ProfileCache, resolve_sender_name

_PROFILE_API = "alice_office_router.channels.line.profiles.AsyncMessagingApi"


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    """Reset the module-level profile cache around every test for isolation."""
    profiles._CACHE._entries.clear()
    yield
    profiles._CACHE._entries.clear()


def _profile(display_name: str) -> SimpleNamespace:
    """Stand in for a LINE (group|room) member profile response.

    Args:
        display_name: The display name the fake profile reports.

    Returns:
        An object exposing a `display_name` attribute like the SDK response.
    """
    return SimpleNamespace(display_name=display_name)


# ---------------------------------------------------------------------------
# resolve_sender_name — lookup, cache, fallback
# ---------------------------------------------------------------------------


async def test_resolves_group_member_display_name() -> None:
    """A group source resolves the speaker's name via the group-member endpoint."""
    mock = AsyncMock(return_value=_profile("王小明"))
    with patch(f"{_PROFILE_API}.get_group_member_profile", new=mock):
        name = await resolve_sender_name("group", "C1", "U9", "token")

    assert name == "王小明"
    mock.assert_awaited_once_with("C1", "U9")


async def test_room_source_uses_room_member_profile() -> None:
    """A room source resolves the name via the room-member endpoint instead."""
    mock = AsyncMock(return_value=_profile("李小華"))
    with patch(f"{_PROFILE_API}.get_room_member_profile", new=mock):
        name = await resolve_sender_name("room", "R1", "U9", "token")

    assert name == "李小華"
    mock.assert_awaited_once_with("R1", "U9")


async def test_second_lookup_is_served_from_cache() -> None:
    """A cache hit returns the name without a second LINE API call."""
    mock = AsyncMock(return_value=_profile("王小明"))
    with patch(f"{_PROFILE_API}.get_group_member_profile", new=mock):
        first = await resolve_sender_name("group", "C1", "U9", "token")
        second = await resolve_sender_name("group", "C1", "U9", "token")

    assert first == second == "王小明"
    assert mock.await_count == 1


async def test_stale_cache_entry_is_refetched() -> None:
    """An entry older than the TTL is dropped and re-fetched from the API."""
    profiles._CACHE._entries[("C1", "U9")] = ("Stale", time.time() - profiles._CACHE._ttl - 1)
    mock = AsyncMock(return_value=_profile("Fresh"))
    with patch(f"{_PROFILE_API}.get_group_member_profile", new=mock):
        name = await resolve_sender_name("group", "C1", "U9", "token")

    assert name == "Fresh"
    mock.assert_awaited_once()


async def test_api_failure_falls_back_to_id_prefix() -> None:
    """A failed profile lookup falls back to the first 8 userId chars (design §8)."""
    mock = AsyncMock(side_effect=ApiException(status=404))
    with patch(f"{_PROFILE_API}.get_group_member_profile", new=mock):
        name = await resolve_sender_name("group", "C1", "U1234567890abc", "token")

    assert name == "U1234567"


@pytest.mark.parametrize(
    "error",
    [ConnectionError("connection refused"), TimeoutError("timed out"), RuntimeError("boom")],
)
async def test_network_error_falls_back_without_raising(error: Exception) -> None:
    """A transient network error (not an ApiException) is absorbed, never raised.

    The line-bot-sdk async client raises ApiException only for non-2xx HTTP
    responses; a connection/DNS/timeout failure surfaces as aiohttp.ClientError
    / asyncio.TimeoutError. resolve_sender_name's "never raises" contract must
    hold for those too, or a profile-API outage would 500 the whole webhook.
    """
    mock = AsyncMock(side_effect=error)
    with patch(f"{_PROFILE_API}.get_group_member_profile", new=mock):
        name = await resolve_sender_name("group", "C1", "U1234567890abc", "token")

    assert name == "U1234567"


async def test_missing_user_id_returns_member_fallback() -> None:
    """With no userId at all the name falls back to the generic member label."""
    name = await resolve_sender_name("group", "C1", None, "token")
    assert name == "成員"


async def test_missing_room_id_falls_back_without_api_call() -> None:
    """Without a group/room id there is nothing to query, so we fall back."""
    mock = AsyncMock(return_value=_profile("王小明"))
    with patch(f"{_PROFILE_API}.get_group_member_profile", new=mock):
        name = await resolve_sender_name("group", None, "U1234567890abc", "token")

    assert name == "U1234567"
    mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# _ProfileCache — TTL + bounded eviction (mirrors dedup.EventDeduplicator)
# ---------------------------------------------------------------------------


class TestProfileCache:
    def test_missing_key_returns_none(self) -> None:
        assert _ProfileCache().get(("C1", "U1")) is None

    def test_put_then_get_returns_value(self) -> None:
        cache = _ProfileCache()
        cache.put(("C1", "U1"), "Name")
        assert cache.get(("C1", "U1")) == "Name"

    def test_entry_past_ttl_is_evicted_on_read(self) -> None:
        cache = _ProfileCache(ttl_seconds=100.0)
        cache._entries[("C1", "U1")] = ("Old", time.time() - 200.0)
        assert cache.get(("C1", "U1")) is None
        assert ("C1", "U1") not in cache._entries

    def test_oldest_tenth_evicted_when_full(self) -> None:
        cache = _ProfileCache(max_size=10)
        for index in range(10):
            cache._entries[(f"C{index}", "U")] = (f"n{index}", float(index))
        cache.put(("Cnew", "U"), "new")

        assert cache.get(("C0", "U")) is None
        assert cache.get(("Cnew", "U")) == "new"
        assert len(cache._entries) <= 10
