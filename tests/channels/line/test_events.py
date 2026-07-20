from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.channels.line.events import (
    Event,
    Mention,
    WebhookBody,
    _strip_self_mentions,
    resolve_inbound_text,
)
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
# Event group helpers — is_group / sender_id / mention_is_self
# ---------------------------------------------------------------------------


class TestEventGroupHelpers:
    def test_group_source_is_group(self) -> None:
        event = Event.model_validate({"source": {"type": "group", "groupId": "C1"}})
        assert event.is_group is True

    def test_room_source_is_group(self) -> None:
        event = Event.model_validate({"source": {"type": "room", "roomId": "R1"}})
        assert event.is_group is True

    def test_user_source_is_not_group(self) -> None:
        event = Event.model_validate({"source": {"type": "user", "userId": "U1"}})
        assert event.is_group is False

    def test_missing_source_is_not_group(self) -> None:
        assert Event.model_validate({}).is_group is False

    def test_sender_id_reads_source_user_id(self) -> None:
        """In a group the speaker is source.userId, distinct from the group id."""
        event = Event.model_validate({"source": {"type": "group", "groupId": "C1", "userId": "U9"}})
        assert event.native_id == "C1"
        assert event.sender_id == "U9"

    def test_sender_id_missing_returns_none(self) -> None:
        assert (
            Event.model_validate({"source": {"type": "group", "groupId": "C1"}}).sender_id is None
        )

    def test_mention_is_self_true_when_bot_mentioned(self) -> None:
        event = Event.model_validate(
            {
                "message": {
                    "type": "text",
                    "text": "@Alice hi",
                    "mention": {"mentionees": [{"index": 0, "length": 6, "isSelf": True}]},
                }
            }
        )
        assert event.mention_is_self is True

    def test_mention_is_self_false_when_other_user_mentioned(self) -> None:
        event = Event.model_validate(
            {
                "message": {
                    "type": "text",
                    "text": "@Bob hi",
                    "mention": {"mentionees": [{"userId": "U2", "isSelf": False}]},
                }
            }
        )
        assert event.mention_is_self is False

    def test_mention_is_self_false_when_no_mention(self) -> None:
        event = Event.model_validate({"message": {"type": "text", "text": "hi"}})
        assert event.mention_is_self is False

    def test_at_all_mention_does_not_count_as_self(self) -> None:
        """An @All mentionee carries type "all" and no isSelf, so it never addresses the bot."""
        event = Event.model_validate(
            {
                "message": {
                    "type": "text",
                    "text": "@All hi",
                    "mention": {"mentionees": [{"index": 0, "length": 4, "type": "all"}]},
                }
            }
        )
        assert event.mention_is_self is False

    def test_mention_is_self_true_when_any_mentionee_is_self(self) -> None:
        event = Event.model_validate(
            {
                "message": {
                    "type": "text",
                    "text": "@Bob @Alice hi",
                    "mention": {
                        "mentionees": [
                            {"userId": "U2", "isSelf": False},
                            {"isSelf": True},
                        ]
                    },
                }
            }
        )
        assert event.mention_is_self is True

    def test_group_non_text_message_is_group_but_not_text(self) -> None:
        """A group sticker event is a group message whose type isn't text (never addressed)."""
        event = Event.model_validate(
            {
                "source": {"type": "group", "groupId": "C1", "userId": "U9"},
                "message": {"type": "sticker", "keywords": ["smile"]},
            }
        )
        assert event.is_group is True
        assert event.mention_is_self is False
        assert event.message is not None
        assert event.message.type == "sticker"


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

    async def test_text_with_self_mention_is_stripped(self) -> None:
        """An "@bot /reset" reaches the agent as the bare command."""
        event = Event.model_validate(
            {
                "message": {
                    "type": "text",
                    "text": "@Alice /reset",
                    "mention": {"mentionees": [{"index": 0, "length": 6, "isSelf": True}]},
                }
            }
        )
        result = await resolve_inbound_text(event, "line_C1", _settings())
        assert result == "/reset"


# ---------------------------------------------------------------------------
# _strip_self_mentions
# ---------------------------------------------------------------------------


def _mention(mentionees: list[dict[str, object]]) -> Mention:
    """Build a Mention from raw mentionee dicts."""
    return Mention.model_validate({"mentionees": mentionees})


class TestStripSelfMentions:
    def test_no_mention_object_returns_text_unchanged(self) -> None:
        assert _strip_self_mentions("hello", None) == "hello"

    def test_removes_self_mention_span(self) -> None:
        text = "@Alice /new"  # "@Alice" is 6 UTF-16 code units
        mention = _mention([{"index": 0, "length": 6, "isSelf": True}])
        assert _strip_self_mentions(text, mention) == "/new"

    def test_keeps_non_self_mention(self) -> None:
        text = "@Bob 早安"
        mention = _mention([{"index": 0, "length": 4, "userId": "U2", "isSelf": False}])
        assert _strip_self_mentions(text, mention) == text

    def test_removes_only_self_keeping_other_mentions(self) -> None:
        text = "@Bob @Alice hi"  # @Bob = [0,4), @Alice = [5,11)
        mention = _mention(
            [
                {"index": 0, "length": 4, "userId": "U2", "isSelf": False},
                {"index": 5, "length": 6, "isSelf": True},
            ]
        )
        assert _strip_self_mentions(text, mention) == "@Bob  hi"

    def test_mention_only_poke_keeps_original_text(self) -> None:
        text = "@Alice"
        mention = _mention([{"index": 0, "length": 6, "isSelf": True}])
        assert _strip_self_mentions(text, mention) == "@Alice"

    def test_span_not_starting_with_at_aborts_stripping(self) -> None:
        """A misaligned span (no leading @) abandons stripping and keeps the text."""
        text = "hello world"
        mention = _mention([{"index": 0, "length": 5, "isSelf": True}])
        assert _strip_self_mentions(text, mention) == "hello world"

    def test_uses_utf16_offsets_past_a_surrogate_pair(self) -> None:
        """An emoji before the mention is 2 UTF-16 units, so offsets must be UTF-16."""
        text = "🍎 @Alice hi"  # 🍎 = units 0-1, space = 2, "@Alice" = [3,9)
        mention = _mention([{"index": 3, "length": 6, "isSelf": True}])
        assert _strip_self_mentions(text, mention) == "🍎  hi"

    def test_span_end_splitting_surrogate_pair_aborts_without_crashing(self) -> None:
        """A span whose end splits a surrogate pair must abort, never raise.

        The span [0,3) covers "@A" plus only the FIRST half of 🍎's surrogate
        pair: the "@" start-guard passes (its decode replaces errors), but the
        leftover half-pair would crash a strict final decode — the strip must
        catch that and keep the original text (the webhook path must not 500).
        """
        text = "@A🍎 hi"  # @=0, A=1, 🍎=units 2-3, space=4
        mention = _mention([{"index": 0, "length": 3, "isSelf": True}])
        assert _strip_self_mentions(text, mention) == "@A🍎 hi"
