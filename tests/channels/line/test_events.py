from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.channels.line.events import Event, WebhookBody, resolve_inbound_text
from alice_office_router.config import Settings

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"

_DOWNLOAD_TARGET = "alice_office_router.channels.line.events.download_line_content"


def _settings(**overrides: object) -> Settings:
    """Build a Settings instance with test credentials, allowing overrides.

    Args:
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance suitable for unit tests.
    """
    defaults: dict[str, object] = {
        "LINE_CHANNEL_SECRET": TEST_SECRET,
        "LINE_CHANNEL_ACCESS_TOKEN": TEST_TOKEN,
        "HERMES_API_SERVER_KEY": "test_api_server_key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Event / Source room-id resolution
# ---------------------------------------------------------------------------


class TestEventRoomId:
    def test_user_source_resolves_native_id_and_prefixed_room_key(self) -> None:
        event = Event.model_validate({"source": {"type": "user", "userId": "U123"}})
        assert event.native_id == "U123"
        assert event.room_key == "line_U123"

    def test_group_source_resolves_group_id(self) -> None:
        event = Event.model_validate({"source": {"type": "group", "groupId": "C123"}})
        assert event.native_id == "C123"
        assert event.room_key == "line_C123"

    def test_missing_source_returns_none(self) -> None:
        assert Event.model_validate({}).native_id is None
        assert Event.model_validate({}).room_key is None

    def test_missing_id_field_returns_none(self) -> None:
        assert Event.model_validate({"source": {"type": "room"}}).native_id is None
        assert Event.model_validate({"source": {"type": "room"}}).room_key is None

    def test_group_source_ignores_extra_user_id(self) -> None:
        """A group source also carrying userId still resolves to the groupId."""
        event = Event.model_validate({"source": {"type": "group", "groupId": "C1", "userId": "U9"}})
        assert event.native_id == "C1"
        assert event.room_key == "line_C1"


# ---------------------------------------------------------------------------
# WebhookBody parsing tolerance
# ---------------------------------------------------------------------------


class TestWebhookBody:
    def test_unknown_event_type_is_retained_not_rejected(self) -> None:
        """LINE may add new event types; they must parse (and be skipped later)."""
        body = WebhookBody.model_validate(
            {"events": [{"type": "unsend", "source": {"type": "user", "userId": "U1"}}]}
        )
        assert len(body.events) == 1
        assert body.events[0].type == "unsend"

    def test_missing_events_key_yields_empty_list(self) -> None:
        assert WebhookBody.model_validate({}).events == []

    def test_non_list_events_yields_empty_list(self) -> None:
        assert WebhookBody.model_validate({"events": "nope"}).events == []

    def test_malformed_event_is_dropped_but_valid_ones_kept(self) -> None:
        """One structurally broken event must not reject the whole batch."""
        body = WebhookBody.model_validate(
            {
                "events": [
                    "not-an-object",
                    {"type": "message", "source": {"type": "user", "userId": "U1"}},
                ]
            }
        )
        assert len(body.events) == 1
        assert body.events[0].native_id == "U1"

    def test_extra_fields_are_ignored(self) -> None:
        body = WebhookBody.model_validate(
            {
                "destination": "Uxxxx",
                "events": [
                    {
                        "type": "message",
                        "mode": "active",
                        "timestamp": 1234567890,
                        "source": {"type": "user", "userId": "U1"},
                        "message": {"type": "text", "text": "hi", "quoteToken": "q"},
                    }
                ],
            }
        )
        assert len(body.events) == 1
        assert body.events[0].message is not None
        assert body.events[0].message.text == "hi"


# ---------------------------------------------------------------------------
# resolve_inbound_text
# ---------------------------------------------------------------------------


class TestResolveInboundText:
    async def test_text_message_returns_text(self) -> None:
        event = Event.model_validate({"message": {"type": "text", "text": "hello"}})
        result = await resolve_inbound_text(event, "room_A", _settings())
        assert result == "hello"

    async def test_blank_text_returns_none(self) -> None:
        event = Event.model_validate({"message": {"type": "text", "text": ""}})
        result = await resolve_inbound_text(event, "room_A", _settings())
        assert result is None

    async def test_no_message_returns_none(self) -> None:
        event = Event.model_validate({"type": "message"})
        result = await resolve_inbound_text(event, "room_A", _settings())
        assert result is None

    async def test_sticker_with_keywords_returns_placeholder(self) -> None:
        event = Event.model_validate(
            {"message": {"type": "sticker", "keywords": ["happy", "smile"]}}
        )
        result = await resolve_inbound_text(event, "room_A", _settings())
        assert result is not None
        assert "happy" in result and "smile" in result

    async def test_sticker_without_keywords_returns_generic_placeholder(self) -> None:
        event = Event.model_validate({"message": {"type": "sticker"}})
        result = await resolve_inbound_text(event, "room_A", _settings())
        assert result == "[使用者傳送了貼圖]"

    async def test_location_returns_placeholder_with_title_and_address(self) -> None:
        event = Event.model_validate(
            {"message": {"type": "location", "title": "台北車站", "address": "台北市中正區"}}
        )
        result = await resolve_inbound_text(event, "room_A", _settings())
        assert result is not None
        assert "台北車站" in result
        assert "台北市中正區" in result

    async def test_unsupported_type_returns_none(self) -> None:
        event = Event.model_validate({"message": {"type": "unknown_future_type"}})
        result = await resolve_inbound_text(event, "room_A", _settings())
        assert result is None

    async def test_image_message_downloads_saves_and_notes_path(self, tmp_path: Path) -> None:
        event = Event.model_validate({"message": {"type": "image", "id": "msg_123"}})
        with patch(_DOWNLOAD_TARGET, new=AsyncMock(return_value=b"fake-jpeg-bytes")):
            result = await resolve_inbound_text(event, "line_room_A", _settings(DATA_DIR=tmp_path))

        # Media is written under the prefixed room_key dir — the one the
        # room's container bind-mounts (see events.py resolve_inbound_text).
        saved = tmp_path / "line_room_A" / "incoming" / "msg_123.jpg"
        assert saved.read_bytes() == b"fake-jpeg-bytes"
        assert result is not None
        assert "/opt/data/incoming/msg_123.jpg" in result

    async def test_file_message_uses_line_provided_filename(self, tmp_path: Path) -> None:
        """A LINE "file" event's real fileName (with its true extension) is used as-is."""
        event = Event.model_validate(
            {
                "message": {
                    "type": "file",
                    "id": "msg_456",
                    "fileName": "report.pdf",
                    "fileSize": 12345,
                }
            }
        )
        with patch(_DOWNLOAD_TARGET, new=AsyncMock(return_value=b"%PDF-1.4 fake")):
            result = await resolve_inbound_text(event, "line_room_A", _settings(DATA_DIR=tmp_path))

        saved = tmp_path / "line_room_A" / "incoming" / "report.pdf"
        assert saved.read_bytes() == b"%PDF-1.4 fake"
        assert result is not None
        assert "/opt/data/incoming/report.pdf" in result

    async def test_file_message_without_filename_falls_back_to_message_id(
        self, tmp_path: Path
    ) -> None:
        event = Event.model_validate({"message": {"type": "file", "id": "msg_789"}})
        with patch(_DOWNLOAD_TARGET, new=AsyncMock(return_value=b"binary")):
            result = await resolve_inbound_text(event, "line_room_A", _settings(DATA_DIR=tmp_path))

        saved = tmp_path / "line_room_A" / "incoming" / "msg_789.bin"
        assert saved.read_bytes() == b"binary"
        assert result is not None

    async def test_file_message_filename_path_traversal_is_stripped(self, tmp_path: Path) -> None:
        """A malicious/unexpected fileName can't escape the room's incoming dir."""
        event = Event.model_validate(
            {"message": {"type": "file", "id": "msg_evil", "fileName": "../../etc/passwd"}}
        )
        with patch(_DOWNLOAD_TARGET, new=AsyncMock(return_value=b"nope")):
            await resolve_inbound_text(event, "line_room_A", _settings(DATA_DIR=tmp_path))

        incoming_dir = tmp_path / "line_room_A" / "incoming"
        assert list(incoming_dir.iterdir()) == [incoming_dir / "passwd"]
        assert not (tmp_path / "etc" / "passwd").exists()

    async def test_media_download_failure_returns_none(self, tmp_path: Path) -> None:
        event = Event.model_validate({"message": {"type": "image", "id": "msg_123"}})
        with patch(_DOWNLOAD_TARGET, new=AsyncMock(side_effect=ApiException(status=404))):
            result = await resolve_inbound_text(event, "line_room_A", _settings(DATA_DIR=tmp_path))

        assert result is None
        assert not (tmp_path / "line_room_A" / "incoming").exists()
