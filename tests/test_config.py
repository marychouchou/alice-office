from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from alice_office_router.config import Settings

_REQUIRED: dict[str, object] = {
    "LINE_CHANNEL_SECRET": "test_secret",
    "LINE_CHANNEL_ACCESS_TOKEN": "test_token",
    "HERMES_API_SERVER_KEY": "test_api_server_key",
}


def test_docker_mode_allows_default_paths() -> None:
    """ROUTER_IN_DOCKER=true (default) must not require DATA_DIR/HERMES_TEMPLATES_DIR."""
    settings = Settings(**_REQUIRED, ROUTER_IN_DOCKER=True)  # type: ignore[arg-type]

    assert Path("/app/data") == settings.DATA_DIR
    assert Path("/app/hermes-templates") == settings.HERMES_TEMPLATES_DIR


def test_host_mode_rejects_default_data_dir() -> None:
    """Host mode with DATA_DIR left at the container-only default must fail fast."""
    with pytest.raises(ValidationError, match="DATA_DIR"):
        Settings(
            **_REQUIRED,  # type: ignore[arg-type]
            ROUTER_IN_DOCKER=False,
            HERMES_TEMPLATES_DIR=Path("/tmp/test_templates"),
        )


def test_host_mode_rejects_default_hermes_templates_dir() -> None:
    """Host mode with HERMES_TEMPLATES_DIR left at the container-only default must fail fast."""
    with pytest.raises(ValidationError, match="HERMES_TEMPLATES_DIR"):
        Settings(
            **_REQUIRED,  # type: ignore[arg-type]
            ROUTER_IN_DOCKER=False,
            DATA_DIR=Path("/tmp/test_data"),
        )


def test_host_mode_accepts_overridden_paths() -> None:
    """Host mode with both paths overridden must construct successfully."""
    settings = Settings(
        **_REQUIRED,  # type: ignore[arg-type]
        ROUTER_IN_DOCKER=False,
        DATA_DIR=Path("/tmp/test_data"),
        HERMES_TEMPLATES_DIR=Path("/tmp/test_templates"),
    )

    assert Path("/tmp/test_data") == settings.DATA_DIR
    assert Path("/tmp/test_templates") == settings.HERMES_TEMPLATES_DIR
