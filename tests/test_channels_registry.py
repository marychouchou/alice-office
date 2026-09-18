from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import APIRouter

from alice_office_router import channels
from alice_office_router.channels import adapter_for, enabled_adapters, register_adapters
from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings


def _settings(**overrides: object) -> Settings:
    """Build a Settings instance with test credentials, allowing overrides.

    Args:
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance suitable for unit tests.
    """
    defaults: dict[str, object] = {
        "LINE_CHANNEL_SECRET": "test_channel_secret",
        "LINE_CHANNEL_ACCESS_TOKEN": "test_channel_access_token",
        "HERMES_API_SERVER_KEY": "test_api_server_key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class _StubAdapter:
    """A minimal ChannelAdapter that records the messages resumed through it."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name
        self.resumed: list[InboundMessage] = []

    def api_router(self) -> APIRouter:
        return APIRouter()

    async def resume(self, msg: InboundMessage) -> None:
        self.resumed.append(msg)


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Restore the process-wide registry after a test replaces it.

    main.py registers the real adapters at import time (conftest imports it),
    so a test that registers stubs must put them back.
    """
    saved = dict(channels._adapters)
    yield
    register_adapters(list(saved.values()))


def test_adapter_for_finds_a_registered_adapter(isolated_registry: None) -> None:
    stub = _StubAdapter()
    register_adapters([stub])

    assert adapter_for("stub") is stub


def test_adapter_for_returns_none_for_an_unknown_channel(isolated_registry: None) -> None:
    register_adapters([_StubAdapter()])

    assert adapter_for("telegram") is None


def test_register_adapters_replaces_the_previous_registration(isolated_registry: None) -> None:
    """A second app's adapters must not leave the first app's behind."""
    register_adapters([_StubAdapter("first")])
    second = _StubAdapter("second")
    register_adapters([second])

    assert adapter_for("first") is None
    assert adapter_for("second") is second


def test_registered_adapters_are_keyed_by_their_own_name(isolated_registry: None) -> None:
    """The key is `adapter.name`, the same value InboundMessage.channel carries."""
    adapters = enabled_adapters(_settings(API_CHANNEL_TOKEN="secret-api-token"))
    register_adapters(adapters)

    assert {"line", "api"} == {adapter.name for adapter in adapters}
    for adapter in adapters:
        assert adapter_for(adapter.name) is adapter


def test_main_registers_the_adapters_it_mounts() -> None:
    """Importing the app is what populates the registry (main.py at app build)."""
    import alice_office_router.main  # noqa: F401

    assert adapter_for("line") is not None
