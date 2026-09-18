from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from alice_office_router.auth_links import (
    AUTH_LINK_ANONYMOUS_NOTICE,
    AUTH_LINKS_DISABLED_NOTICE,
    AUTH_MARKER,
    PENDING_AUTH_TTL_SECONDS,
    publish_auth_links,
    read_pending_auth,
    write_pending_auth,
)
from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"

BASE_URL = "https://router.example.com"
ROOM = "line_U0123456789abcdef0123456789abcdef"
# member_key_for() lowercases the room key for a 1:1 room (account_key).
ROOM_MEMBER = ROOM.lower()

GROUP_ROOM = "line_C0123456789abcdef0123456789abcdef"
SENDER_ID = "U9999999999999999999999999999abcd"
SENDER_MEMBER = SENDER_ID.lower()
SENDER_NAME = "王小明"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Build a Settings instance rooted at tmp_path, with Google OAuth enabled.

    Args:
        tmp_path: Pytest tmp_path fixture, used as DATA_DIR/HOST_DATA_DIR.
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance whose google_oauth_enabled is True unless an
        override (e.g. PUBLIC_BASE_URL="") takes it away.
    """
    defaults: dict[str, object] = {
        "LINE_CHANNEL_SECRET": TEST_SECRET,
        "LINE_CHANNEL_ACCESS_TOKEN": TEST_TOKEN,
        "HERMES_API_SERVER_KEY": "test_api_server_key",
        "DATA_DIR": tmp_path,
        "HOST_DATA_DIR": tmp_path,
        "PUBLIC_BASE_URL": BASE_URL,
    }
    defaults.update(overrides)
    settings = Settings(**defaults)  # type: ignore[arg-type]
    # google_oauth_enabled also needs the deployment's Web client credentials.
    settings.google_web_creds_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_web_creds_path.write_text("{}", encoding="utf-8")
    return settings


def _direct_msg(text: str = "明天有什麼會") -> InboundMessage:
    """Build a 1:1 InboundMessage for the direct-room cases.

    Args:
        text: The inbound plain text.

    Returns:
        An InboundMessage with is_group False.
    """
    return InboundMessage(channel="line", room_key=ROOM, text=text)


def _group_msg(
    *, sender_id: str | None = SENDER_ID, sender_name: str | None = SENDER_NAME
) -> InboundMessage:
    """Build a group InboundMessage, optionally from an unidentified speaker.

    Args:
        sender_id: The speaker's native id, or None when LINE withheld it.
        sender_name: The speaker's display name, or None.

    Returns:
        An InboundMessage with is_group True.
    """
    return InboundMessage(
        channel="line",
        room_key=GROUP_ROOM,
        text="明天有什麼會",
        is_group=True,
        addressed=True,
        sender_id=sender_id,
        sender_name=sender_name,
    )


def _pending_file(settings: Settings, room_id: str, member_key: str) -> Path:
    """Return the path a parked message is written to.

    Args:
        settings: The tmp-rooted Settings under test.
        room_id: The room the message came from.
        member_key: The member the link was issued to.

    Returns:
        The pending_auth JSON path.
    """
    return settings.room_pending_auth_path(room_id, member_key)


# ---------------------------------------------------------------------------
# publish_auth_links
# ---------------------------------------------------------------------------


async def test_a_reply_without_a_marker_is_untouched_and_does_no_io(tmp_path: Path) -> None:
    """The overwhelmingly common reply comes back byte-identical, with zero I/O."""
    settings = _settings(tmp_path)
    text = "今天的重點是三件事，第一……"

    result, requested = await publish_auth_links(text, _direct_msg(), settings)

    assert result == text
    assert requested is False
    # Nothing under the room's own directory was created.
    assert not (tmp_path / ROOM).exists()


async def test_a_direct_room_marker_becomes_that_room_s_own_link(tmp_path: Path) -> None:
    """In a 1:1 room the member is the room, and nobody needs naming."""
    settings = _settings(tmp_path)
    msg = _direct_msg()

    result, requested = await publish_auth_links(
        f"我需要授權才能查：\n{AUTH_MARKER}", msg, settings
    )

    assert requested is True
    assert AUTH_MARKER not in result
    assert f"{BASE_URL}/oauth/start?user_id={ROOM}&member={ROOM_MEMBER}" in result
    # No "<name> " prefix: there is only one person in the room.
    assert SENDER_NAME not in result
    # The question is parked verbatim, so authorizing can re-run it.
    record = json.loads(_pending_file(settings, ROOM, ROOM_MEMBER).read_text(encoding="utf-8"))
    assert InboundMessage.model_validate(record["message"]) == msg
    assert record["ts"] == pytest.approx(time.time(), abs=10)


async def test_a_group_marker_names_the_speaker_and_links_their_own_account(
    tmp_path: Path,
) -> None:
    """A link broadcast to the whole group still says whose it is."""
    settings = _settings(tmp_path)
    msg = _group_msg()

    result, requested = await publish_auth_links(AUTH_MARKER, msg, settings)

    assert requested is True
    assert result.startswith(f"{SENDER_NAME} ")
    assert f"{BASE_URL}/oauth/start?user_id={GROUP_ROOM}&member={SENDER_MEMBER}" in result
    assert _pending_file(settings, GROUP_ROOM, SENDER_MEMBER).exists()


async def test_an_unidentified_group_speaker_gets_a_notice_and_no_pending(
    tmp_path: Path,
) -> None:
    """Without a userId there is no token file to key, so there is no link to give."""
    settings = _settings(tmp_path)

    result, requested = await publish_auth_links(
        f"需要授權：\n{AUTH_MARKER}", _group_msg(sender_id=None, sender_name=None), settings
    )

    assert result == f"需要授權：\n{AUTH_LINK_ANONYMOUS_NOTICE}"
    assert requested is False
    assert "oauth/start" not in result
    assert not (settings.room_router_state_dir(GROUP_ROOM) / "pending_auth").exists()


async def test_a_deployment_without_google_gets_the_disabled_notice(tmp_path: Path) -> None:
    """The container emits the marker regardless; the honest answer lives here."""
    settings = _settings(tmp_path, PUBLIC_BASE_URL="")

    result, requested = await publish_auth_links(f"抱歉，{AUTH_MARKER}", _direct_msg(), settings)

    assert result == f"抱歉，{AUTH_LINKS_DISABLED_NOTICE}"
    assert requested is False
    assert not _pending_file(settings, ROOM, ROOM_MEMBER).exists()


async def test_every_marker_in_one_reply_is_replaced(tmp_path: Path) -> None:
    """Two markers are two copies of the same link, never a leftover placeholder."""
    settings = _settings(tmp_path)

    result, requested = await publish_auth_links(
        f"{AUTH_MARKER}\n中間的說明\n{AUTH_MARKER}", _direct_msg(), settings
    )

    assert requested is True
    assert AUTH_MARKER not in result
    assert result.count(f"{BASE_URL}/oauth/start?user_id={ROOM}&member={ROOM_MEMBER}") == 2


async def test_a_longer_run_of_marker_characters_is_not_a_marker(tmp_path: Path) -> None:
    """Only the exact placeholder is rewritten, so no other text is eaten."""
    settings = _settings(tmp_path)
    text = f"{AUTH_MARKER}ed"

    result, requested = await publish_auth_links(text, _direct_msg(), settings)

    assert result == text
    assert requested is False


# ---------------------------------------------------------------------------
# pending records
# ---------------------------------------------------------------------------


def test_read_pending_auth_returns_none_when_nothing_is_parked(tmp_path: Path) -> None:
    """A member who never triggered the marker is not a special case."""
    settings = _settings(tmp_path)

    assert read_pending_auth(settings, ROOM, ROOM_MEMBER) is None


def test_read_pending_auth_consumes_the_record(tmp_path: Path) -> None:
    """The parked question comes back once, and only once."""
    settings = _settings(tmp_path)
    msg = _direct_msg("幫我看明天的行程")
    write_pending_auth(settings, ROOM, ROOM_MEMBER, msg)

    result = read_pending_auth(settings, ROOM, ROOM_MEMBER)

    assert result == msg
    assert not _pending_file(settings, ROOM, ROOM_MEMBER).exists()
    assert read_pending_auth(settings, ROOM, ROOM_MEMBER) is None


def test_write_pending_auth_keeps_only_the_latest_message(tmp_path: Path) -> None:
    """Triggering the marker twice means waiting on the second question."""
    settings = _settings(tmp_path)
    write_pending_auth(settings, ROOM, ROOM_MEMBER, _direct_msg("第一個問題"))
    write_pending_auth(settings, ROOM, ROOM_MEMBER, _direct_msg("第二個問題"))

    result = read_pending_auth(settings, ROOM, ROOM_MEMBER)

    assert result is not None
    assert result.text == "第二個問題"


def test_an_expired_pending_record_is_dropped_and_logged(tmp_path: Path) -> None:
    """A question asked 11 minutes ago is not re-run behind the user's back."""
    settings = _settings(tmp_path)
    path = _pending_file(settings, ROOM, ROOM_MEMBER)
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = {
        "ts": time.time() - PENDING_AUTH_TTL_SECONDS - 1,
        "message": _direct_msg().model_dump(),
    }
    path.write_text(json.dumps(stale), encoding="utf-8")

    with patch("alice_office_router.auth_links.struct_logger", new=Mock()) as mock_logger:
        result = read_pending_auth(settings, ROOM, ROOM_MEMBER)

    assert result is None
    assert not path.exists()
    assert mock_logger.info.call_args[0] == ("pending_auth_expired",)


def test_a_malformed_pending_record_is_dropped_and_logged(tmp_path: Path) -> None:
    """A truncated or hand-edited record never propagates into the resume path."""
    settings = _settings(tmp_path)
    path = _pending_file(settings, ROOM, ROOM_MEMBER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": 1, "mess', encoding="utf-8")

    with patch("alice_office_router.auth_links.struct_logger", new=Mock()) as mock_logger:
        result = read_pending_auth(settings, ROOM, ROOM_MEMBER)

    assert result is None
    assert not path.exists()
    assert mock_logger.error.call_args[0] == ("pending_auth_malformed",)


def test_a_pending_record_missing_its_message_is_dropped(tmp_path: Path) -> None:
    """Valid JSON with the wrong shape is malformed too, not a crash."""
    settings = _settings(tmp_path)
    path = _pending_file(settings, ROOM, ROOM_MEMBER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": 1}', encoding="utf-8")

    with patch("alice_office_router.auth_links.struct_logger", new=Mock()) as mock_logger:
        result = read_pending_auth(settings, ROOM, ROOM_MEMBER)

    assert result is None
    assert mock_logger.error.call_args[0] == ("pending_auth_malformed",)
