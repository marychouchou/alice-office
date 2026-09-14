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


# ---------------------------------------------------------------------------
# Group-chat settings + path helper
# ---------------------------------------------------------------------------


def test_group_defaults() -> None:
    """GROUP_* fields default to no call-words and a 50-message observed cap."""
    settings = Settings(**_REQUIRED)  # type: ignore[arg-type]

    assert settings.GROUP_TRIGGER_PREFIXES == ""
    assert settings.group_trigger_prefixes() == ()
    assert settings.GROUP_OBSERVED_MAX_MESSAGES == 50


def test_group_trigger_prefixes_parses_and_strips() -> None:
    """The comma-separated prefixes are split, stripped, and blanks dropped."""
    settings = Settings(**_REQUIRED, GROUP_TRIGGER_PREFIXES="小幫手, Alice ,")  # type: ignore[arg-type]

    assert settings.group_trigger_prefixes() == ("小幫手", "Alice")


def test_room_group_state_dir_path() -> None:
    """room_group_state_dir hangs the observed buffer under DATA_DIR/<room_id>."""
    settings = Settings(**_REQUIRED, DATA_DIR=Path("/data"))  # type: ignore[arg-type]

    assert settings.room_group_state_dir("line_C1") == Path("/data/line_C1/group_state")


def test_log_level_rejects_garbage_with_a_pydantic_error() -> None:
    """A typo'd LOG_LEVEL must fail at Settings construction, naming the field.

    Before this was a Literal, the bad value travelled all the way into
    logging.config.dictConfig and surfaced as a traceback about an unknown
    level, with nothing pointing at the setting that caused it.
    """
    with pytest.raises(ValidationError) as error_info:
        Settings(**_REQUIRED, LOG_LEVEL="VERBOSE")  # type: ignore[arg-type]

    message = str(error_info.value)
    assert "LOG_LEVEL" in message
    assert "DEBUG" in message and "CRITICAL" in message


def test_log_level_accepts_lower_case() -> None:
    """`LOG_LEVEL=debug` in a shell is the same setting as DEBUG."""
    settings = Settings(**_REQUIRED, LOG_LEVEL="debug")  # type: ignore[arg-type]

    assert settings.LOG_LEVEL == "DEBUG"
