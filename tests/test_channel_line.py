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

from alice_office_router.channels.base import InboundMessage
from alice_office_router.channels.line import LineResponder
from alice_office_router.channels.pipeline import process_inbound
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
def override_settings_dep() -> None:
    """Override the settings dependency on the FastAPI app for test isolation."""
    from alice_office_router.config import get_settings
    from alice_office_router.main import app

    app.dependency_overrides[get_settings] = lambda: _settings()
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
    with patch("alice_office_router.channels.line.process_inbound", new_callable=AsyncMock):
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
    """A webhook body with multiple message events schedules a pipeline run for each."""
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
        "alice_office_router.channels.line.process_inbound", new_callable=AsyncMock
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
    async def test_schedules_pipeline_run_for_text_message(self) -> None:
        from alice_office_router.channels.line import _dispatch_event
        from alice_office_router.line_events import Event

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
        assert task.func is process_inbound
        message, responder = task.args[0], task.args[1]
        assert message == InboundMessage(channel="line", room_id="U1", text="hi")
        assert isinstance(responder, LineResponder)
        assert responder.room_id == "U1"
        assert responder.reply_token == "reply_1"

    async def test_ignores_non_message_event_types(self) -> None:
        from alice_office_router.channels.line import _dispatch_event
        from alice_office_router.line_events import Event

        event = Event.model_validate({"type": "follow", "source": {"type": "user", "userId": "U1"}})
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks == []

    async def test_duplicate_webhook_event_id_is_skipped(self) -> None:
        from alice_office_router.channels.line import _dispatch_event
        from alice_office_router.line_events import Event

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
        from alice_office_router.channels.line import _dispatch_event
        from alice_office_router.line_events import Event

        event = Event.model_validate(
            {"type": "message", "source": {}, "message": {"type": "text", "text": "hi"}}
        )
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks == []

    async def test_missing_reply_token_passes_none(self) -> None:
        from alice_office_router.channels.line import _dispatch_event
        from alice_office_router.line_events import Event

        event = Event.model_validate(
            {
                "type": "message",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "hi"},
            }
        )
        background_tasks = BackgroundTasks()
        await _dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks[0].args[1].reply_token is None


# ---------------------------------------------------------------------------
# LineResponder — reply-token-first, Push fallback
# ---------------------------------------------------------------------------


class TestLineResponder:
    async def test_uses_reply_token_when_present(self) -> None:
        responder = LineResponder("room_A", "reply_token_1", TEST_TOKEN)

        with (
            patch(
                "alice_office_router.channels.line.reply_line_message", new=AsyncMock()
            ) as mock_reply,
            patch(
                "alice_office_router.channels.line.push_line_message", new=AsyncMock()
            ) as mock_push,
        ):
            await responder.send_reply("hello")

        mock_reply.assert_awaited_once_with("reply_token_1", "hello", TEST_TOKEN)
        mock_push.assert_not_called()

    async def test_falls_back_to_push_when_reply_token_rejected(self) -> None:
        responder = LineResponder("room_A", "expired_token", TEST_TOKEN)

        with (
            patch(
                "alice_office_router.channels.line.reply_line_message",
                new=AsyncMock(side_effect=ApiException(status=400)),
            ) as mock_reply,
            patch(
                "alice_office_router.channels.line.push_line_message", new=AsyncMock()
            ) as mock_push,
        ):
            await responder.send_reply("hello")

        mock_reply.assert_awaited_once()
        mock_push.assert_awaited_once_with("room_A", "hello", TEST_TOKEN)

    async def test_pushes_directly_when_no_reply_token(self) -> None:
        responder = LineResponder("room_A", None, TEST_TOKEN)

        with (
            patch(
                "alice_office_router.channels.line.reply_line_message", new=AsyncMock()
            ) as mock_reply,
            patch(
                "alice_office_router.channels.line.push_line_message", new=AsyncMock()
            ) as mock_push,
        ):
            await responder.send_reply("hello")

        mock_reply.assert_not_called()
        mock_push.assert_awaited_once_with("room_A", "hello", TEST_TOKEN)

    async def test_reply_token_is_single_use(self) -> None:
        """A second send_reply must not reuse the (single-use) reply token."""
        responder = LineResponder("room_A", "reply_token_1", TEST_TOKEN)

        with (
            patch(
                "alice_office_router.channels.line.reply_line_message", new=AsyncMock()
            ) as mock_reply,
            patch(
                "alice_office_router.channels.line.push_line_message", new=AsyncMock()
            ) as mock_push,
        ):
            await responder.send_reply("first")
            await responder.send_reply("second")

        mock_reply.assert_awaited_once_with("reply_token_1", "first", TEST_TOKEN)
        mock_push.assert_awaited_once_with("room_A", "second", TEST_TOKEN)

    async def test_send_notice_pushes_and_keeps_reply_token(self) -> None:
        """Notices go out via Push so the reply token stays available for the reply."""
        responder = LineResponder("room_A", "reply_token_1", TEST_TOKEN)

        with (
            patch(
                "alice_office_router.channels.line.reply_line_message", new=AsyncMock()
            ) as mock_reply,
            patch(
                "alice_office_router.channels.line.push_line_message", new=AsyncMock()
            ) as mock_push,
        ):
            await responder.send_notice("notice")

        mock_reply.assert_not_called()
        mock_push.assert_awaited_once_with("room_A", "notice", TEST_TOKEN)
        assert responder.reply_token == "reply_token_1"
