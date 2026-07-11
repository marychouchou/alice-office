from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks
from httpx import AsyncClient
from linebot.v3.messaging.exceptions import ApiException

from alice_office_router.config import Settings

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"


def _sign(body: bytes) -> str:
    """Compute a valid LINE HMAC-SHA256 signature for a raw webhook body.

    Args:
        body: Raw JSON body bytes.

    Returns:
        Base64-encoded signature string.
    """
    digest = hmac_mod.new(TEST_SECRET.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


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


@pytest.fixture(autouse=True)
def patch_settings() -> None:
    """Patch get_settings to return test credentials for all router tests."""
    from alice_office_router.config import Settings

    mock_settings = Settings(
        LINE_CHANNEL_SECRET=TEST_SECRET,
        LINE_CHANNEL_ACCESS_TOKEN=TEST_TOKEN,
        HERMES_API_SERVER_KEY="test_api_server_key",
    )
    with patch("alice_office_router.router.get_settings", return_value=mock_settings):
        pass


@pytest.fixture(autouse=True)
def override_settings_dep() -> None:
    """Override the settings dependency on the FastAPI app for test isolation."""
    from alice_office_router.config import Settings, get_settings
    from alice_office_router.main import app

    def _test_settings() -> Settings:
        return Settings(
            LINE_CHANNEL_SECRET=TEST_SECRET,
            LINE_CHANNEL_ACCESS_TOKEN=TEST_TOKEN,
            HERMES_API_SERVER_KEY="test_api_server_key",
        )

    app.dependency_overrides[get_settings] = _test_settings
    yield  # type: ignore[misc]
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Webhook endpoint — envelope-level behavior
# ---------------------------------------------------------------------------


async def test_missing_signature_returns_400(client: AsyncClient, line_webhook_body: bytes) -> None:
    """POST /webhook without x-line-signature header should return 400."""
    response = await client.post(
        "/webhook",
        content=line_webhook_body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


async def test_invalid_signature_returns_400(client: AsyncClient, line_webhook_body: bytes) -> None:
    """POST /webhook with a wrong signature should return 400."""
    response = await client.post(
        "/webhook",
        content=line_webhook_body,
        headers={
            "Content-Type": "application/json",
            "x-line-signature": "invalidsignature==",
        },
    )
    assert response.status_code == 400


async def test_valid_request_returns_200_ok(
    client: AsyncClient,
    line_webhook_body: bytes,
    valid_signature: str,
) -> None:
    """POST /webhook with a valid signature and body should return 200 {"status": "ok"}."""
    with (
        patch(
            "alice_office_router.router.get_or_create_container",
            return_value="http://hermes_room_TEST123:8642",
        ),
        patch(
            "alice_office_router.router._process_and_reply",
            new_callable=AsyncMock,
        ),
    ):
        response = await client.post(
            "/webhook",
            content=line_webhook_body,
            headers={
                "Content-Type": "application/json",
                "x-line-signature": valid_signature,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_empty_events_returns_200_ok(
    client: AsyncClient,
    valid_signature: str,
) -> None:
    """POST /webhook with empty events list should return 200 without dispatching."""
    empty_body = json.dumps({"events": []}).encode("utf-8")

    response = await client.post(
        "/webhook",
        content=empty_body,
        headers={
            "Content-Type": "application/json",
            "x-line-signature": _sign(empty_body),
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_missing_room_id_returns_400(
    client: AsyncClient,
) -> None:
    """POST /webhook where source has no roomId/userId/groupId should return 400."""
    bad_body = json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "source": {"type": "room"},  # roomId missing
                    "message": {"type": "text", "text": "hi"},
                }
            ]
        }
    ).encode("utf-8")

    response = await client.post(
        "/webhook",
        content=bad_body,
        headers={"Content-Type": "application/json", "x-line-signature": _sign(bad_body)},
    )
    assert response.status_code == 400


async def test_multiple_events_are_all_dispatched(
    client: AsyncClient,
) -> None:
    """A webhook body with multiple message events schedules a task for each."""
    body = json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "source": {"type": "user", "userId": "U1"},
                    "message": {"type": "text", "text": "one"},
                },
                {
                    "type": "message",
                    "source": {"type": "user", "userId": "U2"},
                    "message": {"type": "text", "text": "two"},
                },
            ]
        }
    ).encode("utf-8")

    with patch(
        "alice_office_router.router._process_and_reply", new_callable=AsyncMock
    ) as mock_process:
        response = await client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "x-line-signature": _sign(body)},
        )

    assert response.status_code == 200
    assert mock_process.await_count == 2


# ---------------------------------------------------------------------------
# _resolve_room_id
# ---------------------------------------------------------------------------


class TestResolveRoomId:
    def test_user_source_resolves_user_id(self) -> None:
        from alice_office_router.router import _resolve_room_id

        event = {"source": {"type": "user", "userId": "U123"}}
        assert _resolve_room_id(event) == "U123"

    def test_group_source_resolves_group_id(self) -> None:
        from alice_office_router.router import _resolve_room_id

        event = {"source": {"type": "group", "groupId": "C123"}}
        assert _resolve_room_id(event) == "C123"

    def test_missing_source_returns_none(self) -> None:
        from alice_office_router.router import _resolve_room_id

        assert _resolve_room_id({}) is None

    def test_missing_id_field_returns_none(self) -> None:
        from alice_office_router.router import _resolve_room_id

        assert _resolve_room_id({"source": {"type": "room"}}) is None


# ---------------------------------------------------------------------------
# _resolve_inbound_text
# ---------------------------------------------------------------------------


class TestResolveInboundText:
    async def test_text_message_returns_text(self) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "text", "text": "hello"}}
        result = await _resolve_inbound_text(event, "room_A", _settings())
        assert result == "hello"

    async def test_blank_text_returns_none(self) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "text", "text": ""}}
        result = await _resolve_inbound_text(event, "room_A", _settings())
        assert result is None

    async def test_sticker_with_keywords_returns_placeholder(self) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "sticker", "keywords": ["happy", "smile"]}}
        result = await _resolve_inbound_text(event, "room_A", _settings())
        assert result is not None
        assert "happy" in result and "smile" in result

    async def test_sticker_without_keywords_returns_generic_placeholder(self) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "sticker"}}
        result = await _resolve_inbound_text(event, "room_A", _settings())
        assert result == "[使用者傳送了貼圖]"

    async def test_location_returns_placeholder_with_title_and_address(self) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "location", "title": "台北車站", "address": "台北市中正區"}}
        result = await _resolve_inbound_text(event, "room_A", _settings())
        assert result is not None
        assert "台北車站" in result
        assert "台北市中正區" in result

    async def test_unsupported_type_returns_none(self) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "unknown_future_type"}}
        result = await _resolve_inbound_text(event, "room_A", _settings())
        assert result is None

    async def test_image_message_downloads_saves_and_notes_path(self, tmp_path: Path) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "image", "id": "msg_123"}}
        with patch(
            "alice_office_router.router.download_line_content",
            new=AsyncMock(return_value=b"fake-jpeg-bytes"),
        ):
            result = await _resolve_inbound_text(event, "room_A", _settings(DATA_DIR=tmp_path))

        saved = tmp_path / "room_A" / "incoming" / "msg_123.jpg"
        assert saved.read_bytes() == b"fake-jpeg-bytes"
        assert result is not None
        assert "/opt/data/incoming/msg_123.jpg" in result

    async def test_file_message_uses_line_provided_filename(self, tmp_path: Path) -> None:
        """A LINE "file" event's real fileName (with its true extension) is used as-is."""
        from alice_office_router.router import _resolve_inbound_text

        event = {
            "message": {
                "type": "file",
                "id": "msg_456",
                "fileName": "report.pdf",
                "fileSize": 12345,
            }
        }
        with patch(
            "alice_office_router.router.download_line_content",
            new=AsyncMock(return_value=b"%PDF-1.4 fake"),
        ):
            result = await _resolve_inbound_text(event, "room_A", _settings(DATA_DIR=tmp_path))

        saved = tmp_path / "room_A" / "incoming" / "report.pdf"
        assert saved.read_bytes() == b"%PDF-1.4 fake"
        assert result is not None
        assert "/opt/data/incoming/report.pdf" in result

    async def test_file_message_without_filename_falls_back_to_message_id(
        self, tmp_path: Path
    ) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "file", "id": "msg_789"}}
        with patch(
            "alice_office_router.router.download_line_content",
            new=AsyncMock(return_value=b"binary"),
        ):
            result = await _resolve_inbound_text(event, "room_A", _settings(DATA_DIR=tmp_path))

        saved = tmp_path / "room_A" / "incoming" / "msg_789.bin"
        assert saved.read_bytes() == b"binary"
        assert result is not None

    async def test_file_message_filename_path_traversal_is_stripped(self, tmp_path: Path) -> None:
        """A malicious/unexpected fileName can't escape the room's incoming dir."""
        from alice_office_router.router import _resolve_inbound_text

        event = {
            "message": {
                "type": "file",
                "id": "msg_evil",
                "fileName": "../../etc/passwd",
            }
        }
        with patch(
            "alice_office_router.router.download_line_content",
            new=AsyncMock(return_value=b"nope"),
        ):
            await _resolve_inbound_text(event, "room_A", _settings(DATA_DIR=tmp_path))

        incoming_dir = tmp_path / "room_A" / "incoming"
        assert list(incoming_dir.iterdir()) == [incoming_dir / "passwd"]
        assert not (tmp_path / "etc" / "passwd").exists()

    async def test_media_download_failure_returns_none(self, tmp_path: Path) -> None:
        from alice_office_router.router import _resolve_inbound_text

        event = {"message": {"type": "image", "id": "msg_123"}}
        with patch(
            "alice_office_router.router.download_line_content",
            new=AsyncMock(side_effect=ApiException(status=404)),
        ):
            result = await _resolve_inbound_text(event, "room_A", _settings(DATA_DIR=tmp_path))

        assert result is None
        assert not (tmp_path / "room_A" / "incoming").exists()


# ---------------------------------------------------------------------------
# _dispatch_event
# ---------------------------------------------------------------------------


class TestDispatchEvent:
    async def test_schedules_task_for_text_message(self) -> None:
        from alice_office_router.router import _dispatch_event

        event = {
            "type": "message",
            "webhookEventId": "evt_1",
            "replyToken": "reply_1",
            "source": {"type": "user", "userId": "U1"},
            "message": {"type": "text", "text": "hi"},
        }
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert len(background_tasks.tasks) == 1
        task = background_tasks.tasks[0]
        # room_id and text are the first two positional args.
        assert task.args[0] == "U1"
        assert task.args[1] == "hi"
        assert task.args[3] == "reply_1"

    async def test_ignores_non_message_event_types(self) -> None:
        from alice_office_router.router import _dispatch_event

        event = {"type": "follow", "source": {"type": "user", "userId": "U1"}}
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks == []

    async def test_duplicate_webhook_event_id_is_skipped(self) -> None:
        from alice_office_router.router import _dispatch_event

        event = {
            "type": "message",
            "webhookEventId": "evt_dup",
            "source": {"type": "user", "userId": "U1"},
            "message": {"type": "text", "text": "hi"},
        }
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())
        await _dispatch_event(event, background_tasks, _settings())

        assert len(background_tasks.tasks) == 1

    async def test_unresolvable_room_id_is_skipped(self) -> None:
        from alice_office_router.router import _dispatch_event

        event = {"type": "message", "source": {}, "message": {"type": "text", "text": "hi"}}
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks == []

    async def test_missing_reply_token_passes_none(self) -> None:
        from alice_office_router.router import _dispatch_event

        event = {
            "type": "message",
            "source": {"type": "user", "userId": "U1"},
            "message": {"type": "text", "text": "hi"},
        }
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks[0].args[3] is None


# ---------------------------------------------------------------------------
# _deliver_reply — reply-token-first, Push fallback
# ---------------------------------------------------------------------------


class TestDeliverReply:
    async def test_uses_reply_token_when_present(self) -> None:
        from alice_office_router.router import _deliver_reply

        with (
            patch("alice_office_router.router.reply_line_message", new=AsyncMock()) as mock_reply,
            patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await _deliver_reply("room_A", "hello", "reply_token_1", _settings())

        mock_reply.assert_awaited_once_with("reply_token_1", "hello", TEST_TOKEN)
        mock_push.assert_not_called()

    async def test_falls_back_to_push_when_reply_token_rejected(self) -> None:
        from alice_office_router.router import _deliver_reply

        with (
            patch(
                "alice_office_router.router.reply_line_message",
                new=AsyncMock(side_effect=ApiException(status=400)),
            ) as mock_reply,
            patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await _deliver_reply("room_A", "hello", "expired_token", _settings())

        mock_reply.assert_awaited_once()
        mock_push.assert_awaited_once_with("room_A", "hello", TEST_TOKEN)

    async def test_pushes_directly_when_no_reply_token(self) -> None:
        from alice_office_router.router import _deliver_reply

        with (
            patch("alice_office_router.router.reply_line_message", new=AsyncMock()) as mock_reply,
            patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await _deliver_reply("room_A", "hello", None, _settings())

        mock_reply.assert_not_called()
        mock_push.assert_awaited_once_with("room_A", "hello", TEST_TOKEN)


# ---------------------------------------------------------------------------
# _process_and_reply
# ---------------------------------------------------------------------------


async def test_process_and_reply_pushes_agent_response_to_line() -> None:
    """_process_and_reply resolves the container, asks the agent, and delivers the reply."""
    from alice_office_router.router import _process_and_reply

    settings = _settings()

    with (
        patch(
            "alice_office_router.router.get_or_create_container",
            return_value="http://hermes_room_AAA:8642",
        ) as mock_get_container,
        patch(
            "alice_office_router.router.ask_hermes_agent",
            new=AsyncMock(return_value="哈囉，我是 Hermes"),
        ) as mock_ask,
        patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings)

    mock_get_container.assert_called_once_with("room_AAA", settings)
    mock_ask.assert_awaited_once_with(
        "http://hermes_room_AAA:8642", "room_AAA", "哈囉", "test_api_server_key"
    )
    mock_push.assert_awaited_once_with("room_AAA", "哈囉，我是 Hermes", TEST_TOKEN)


async def test_process_and_reply_uses_reply_token_when_provided() -> None:
    from alice_office_router.router import _process_and_reply

    settings = _settings()

    with (
        patch(
            "alice_office_router.router.get_or_create_container",
            return_value="http://hermes_room_AAA:8642",
        ),
        patch(
            "alice_office_router.router.ask_hermes_agent",
            new=AsyncMock(return_value="哈囉"),
        ),
        patch("alice_office_router.router.reply_line_message", new=AsyncMock()) as mock_reply,
        patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings, "reply_token_1")

    mock_reply.assert_awaited_once()
    mock_push.assert_not_called()


async def test_process_and_reply_logs_and_returns_on_agent_error() -> None:
    """A Hermes agent failure is logged; it must not raise or push anything to LINE."""
    from alice_office_router.router import _process_and_reply

    settings = _settings()

    with (
        patch(
            "alice_office_router.router.get_or_create_container",
            return_value="http://hermes_room_AAA:8642",
        ),
        patch(
            "alice_office_router.router.ask_hermes_agent",
            new=AsyncMock(side_effect=ValueError("boom")),
        ),
        patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings)

    mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# _process_and_reply — Google OAuth gate
# ---------------------------------------------------------------------------


class TestProcessAndReplyGoogleGate:
    async def test_blocked_delivers_auth_message_and_never_calls_agent(self) -> None:
        from alice_office_router.router import _process_and_reply

        settings = _settings()

        with (
            patch(
                "alice_office_router.router.check_google_authorization",
                return_value=("blocked", "請先授權 Google 帳號：https://example.com/oauth/start?user_id=room_aaa"),
            ),
            patch("alice_office_router.router.get_or_create_container") as mock_get_container,
            patch(
                "alice_office_router.router.ask_hermes_agent", new=AsyncMock()
            ) as mock_ask,
            patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await _process_and_reply("room_AAA", "哈囉", settings)

        mock_get_container.assert_not_called()
        mock_ask.assert_not_awaited()
        mock_push.assert_awaited_once()
        assert "oauth/start" in mock_push.await_args.args[1]

    async def test_ok_status_proceeds_with_normal_flow(self) -> None:
        from alice_office_router.router import _process_and_reply

        settings = _settings()

        with (
            patch(
                "alice_office_router.router.check_google_authorization",
                return_value=("ok", None),
            ),
            patch(
                "alice_office_router.router.get_or_create_container",
                return_value="http://hermes_room_AAA:8642",
            ) as mock_get_container,
            patch(
                "alice_office_router.router.ask_hermes_agent",
                new=AsyncMock(return_value="哈囉，我是 Hermes"),
            ),
            patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await _process_and_reply("room_AAA", "哈囉", settings)

        mock_get_container.assert_called_once_with("room_AAA", settings)
        mock_push.assert_awaited_once_with("room_AAA", "哈囉，我是 Hermes", TEST_TOKEN)

    async def test_notice_status_pushes_notice_and_still_calls_agent(self) -> None:
        from alice_office_router.router import _process_and_reply

        settings = _settings()

        with (
            patch(
                "alice_office_router.router.check_google_authorization",
                return_value=("notice", "缺少 Drive 授權：https://example.com/oauth/start?user_id=room_aaa"),
            ),
            patch(
                "alice_office_router.router.get_or_create_container",
                return_value="http://hermes_room_AAA:8642",
            ) as mock_get_container,
            patch(
                "alice_office_router.router.ask_hermes_agent",
                new=AsyncMock(return_value="哈囉，我是 Hermes"),
            ),
            patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await _process_and_reply("room_AAA", "哈囉", settings)

        mock_get_container.assert_called_once()
        assert mock_push.await_count == 2
        first_call_text = mock_push.await_args_list[0].args[1]
        assert "oauth/start" in first_call_text
        second_call_text = mock_push.await_args_list[1].args[1]
        assert second_call_text == "哈囉，我是 Hermes"
