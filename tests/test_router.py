from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
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
    with patch(
        "alice_office_router.router._process_and_reply",
        new_callable=AsyncMock,
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
# _dispatch_event
# ---------------------------------------------------------------------------


class TestDispatchEvent:
    async def test_schedules_task_for_text_message(self) -> None:
        from alice_office_router.line_events import Event
        from alice_office_router.router import _dispatch_event

        event = Event.model_validate(
            {
                "type": "message",
                "webhookEventId": "evt_1",
                "replyToken": "reply_1",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "hi"},
            }
        )
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert len(background_tasks.tasks) == 1
        task = background_tasks.tasks[0]
        # room_id and text are the first two positional args.
        assert task.args[0] == "U1"
        assert task.args[1] == "hi"
        assert task.args[3] == "reply_1"

    async def test_ignores_non_message_event_types(self) -> None:
        from alice_office_router.line_events import Event
        from alice_office_router.router import _dispatch_event

        event = Event.model_validate({"type": "follow", "source": {"type": "user", "userId": "U1"}})
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks == []

    async def test_duplicate_webhook_event_id_is_skipped(self) -> None:
        from alice_office_router.line_events import Event
        from alice_office_router.router import _dispatch_event

        event = Event.model_validate(
            {
                "type": "message",
                "webhookEventId": "evt_dup",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "hi"},
            }
        )
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())
        await _dispatch_event(event, background_tasks, _settings())

        assert len(background_tasks.tasks) == 1

    async def test_unresolvable_room_id_is_skipped(self) -> None:
        from alice_office_router.line_events import Event
        from alice_office_router.router import _dispatch_event

        event = Event.model_validate(
            {"type": "message", "source": {}, "message": {"type": "text", "text": "hi"}}
        )
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks == []

    async def test_missing_reply_token_passes_none(self) -> None:
        from alice_office_router.line_events import Event
        from alice_office_router.router import _dispatch_event

        event = Event.model_validate(
            {
                "type": "message",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "hi"},
            }
        )
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
# _process_and_reply — delivery wiring (orchestration lives in core.process_inbound)
# ---------------------------------------------------------------------------


async def test_process_and_reply_pushes_single_text_when_no_reply_token() -> None:
    """A single reply text with no reply token is delivered via Push."""
    from alice_office_router.router import _process_and_reply

    settings = _settings()

    with (
        patch(
            "alice_office_router.router.process_inbound",
            new=AsyncMock(return_value=["哈囉，我是 Hermes"]),
        ),
        patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings)

    mock_push.assert_awaited_once_with("room_AAA", "哈囉，我是 Hermes", TEST_TOKEN)


async def test_process_and_reply_uses_reply_token_for_first_text() -> None:
    """The first text uses the reply token; Push is not called for it."""
    from alice_office_router.router import _process_and_reply

    settings = _settings()

    with (
        patch(
            "alice_office_router.router.process_inbound",
            new=AsyncMock(return_value=["哈囉"]),
        ),
        patch("alice_office_router.router.reply_line_message", new=AsyncMock()) as mock_reply,
        patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings, "reply_token_1")

    mock_reply.assert_awaited_once()
    mock_push.assert_not_called()


async def test_process_and_reply_first_text_reply_token_rest_push() -> None:
    """With multiple texts, only the first uses the reply token; the rest are pushed."""
    from alice_office_router.router import _process_and_reply

    settings = _settings()

    with (
        patch(
            "alice_office_router.router.process_inbound",
            new=AsyncMock(return_value=["notice", "agent reply"]),
        ),
        patch("alice_office_router.router.reply_line_message", new=AsyncMock()) as mock_reply,
        patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings, "reply_token_1")

    mock_reply.assert_awaited_once_with("reply_token_1", "notice", TEST_TOKEN)
    mock_push.assert_awaited_once_with("room_AAA", "agent reply", TEST_TOKEN)


async def test_process_and_reply_delivers_nothing_on_empty_texts() -> None:
    """When core returns no texts (e.g. agent failure), nothing is sent to LINE."""
    from alice_office_router.router import _process_and_reply

    settings = _settings()

    with (
        patch(
            "alice_office_router.router.process_inbound",
            new=AsyncMock(return_value=[]),
        ),
        patch("alice_office_router.router.reply_line_message", new=AsyncMock()) as mock_reply,
        patch("alice_office_router.router.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await _process_and_reply("room_AAA", "哈囉", settings, "reply_token_1")

    mock_reply.assert_not_called()
    mock_push.assert_not_called()
