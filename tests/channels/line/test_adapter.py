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
from alice_office_router.channels.line.adapter import LineAdapter
from alice_office_router.channels.line.events import Event
from alice_office_router.config import Settings

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"

_ADAPTER = "alice_office_router.channels.line.adapter"
_BOTH_WEBHOOK_PATHS = ["/webhook", "/webhooks/line"]


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

    def _test_settings() -> Settings:
        return _settings()

    app.dependency_overrides[get_settings] = _test_settings
    yield  # type: ignore[misc]
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Webhook endpoint — envelope-level behavior (both the legacy /webhook alias
# and the canonical /webhooks/line path share one handler)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _BOTH_WEBHOOK_PATHS)
async def test_missing_signature_returns_400(
    client: AsyncClient, line_webhook_body: bytes, path: str
) -> None:
    """POST without x-line-signature header should return 400 on both paths."""
    response = await client.post(
        path,
        content=line_webhook_body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("path", _BOTH_WEBHOOK_PATHS)
async def test_invalid_signature_returns_400(
    client: AsyncClient, line_webhook_body: bytes, path: str
) -> None:
    """POST with a wrong signature should return 400 on both paths."""
    response = await client.post(
        path,
        content=line_webhook_body,
        headers={
            "Content-Type": "application/json",
            "x-line-signature": "invalidsignature==",
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("path", _BOTH_WEBHOOK_PATHS)
async def test_valid_request_returns_200_ok(
    client: AsyncClient,
    line_webhook_body: bytes,
    valid_signature: str,
    path: str,
) -> None:
    """A valid signed body should return 200 {"status": "ok"} on both paths."""
    with patch.object(LineAdapter, "_process_and_reply", new_callable=AsyncMock):
        response = await client.post(
            path,
            content=line_webhook_body,
            headers={
                "Content-Type": "application/json",
                "x-line-signature": valid_signature,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", _BOTH_WEBHOOK_PATHS)
async def test_empty_events_returns_200_ok(
    client: AsyncClient,
    valid_signature: str,
    path: str,
) -> None:
    """An empty events list should return 200 without dispatching, on both paths."""
    empty_body = json.dumps({"events": []}).encode("utf-8")

    response = await client.post(
        path,
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
    """POST where source has no roomId/userId/groupId should return 400."""
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
        "/webhooks/line",
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

    with patch.object(LineAdapter, "_process_and_reply", new_callable=AsyncMock) as mock_process:
        response = await client.post(
            "/webhooks/line",
            content=body,
            headers={"Content-Type": "application/json", "x-line-signature": _sign(body)},
        )

    assert response.status_code == 200
    assert mock_process.await_count == 2


# ---------------------------------------------------------------------------
# LineAdapter._dispatch_event
# ---------------------------------------------------------------------------


class TestDispatchEvent:
    async def test_schedules_task_for_text_message(self) -> None:
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
        await LineAdapter()._dispatch_event(event, background_tasks, _settings())

        assert len(background_tasks.tasks) == 1
        task = background_tasks.tasks[0]
        # room_key (prefixed) and text are the first two positional args.
        assert task.args[0] == "line_U1"
        assert task.args[1] == "hi"
        assert task.args[3] == "reply_1"

    async def test_ignores_non_message_event_types(self) -> None:
        event = Event.model_validate({"type": "follow", "source": {"type": "user", "userId": "U1"}})
        background_tasks = BackgroundTasks()
        await LineAdapter()._dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks == []

    async def test_duplicate_webhook_event_id_is_skipped(self) -> None:
        event = Event.model_validate(
            {
                "type": "message",
                "webhookEventId": "evt_dup",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "hi"},
            }
        )
        adapter = LineAdapter()
        background_tasks = BackgroundTasks()
        await adapter._dispatch_event(event, background_tasks, _settings())
        await adapter._dispatch_event(event, background_tasks, _settings())

        assert len(background_tasks.tasks) == 1

    async def test_unresolvable_room_id_is_skipped(self) -> None:
        event = Event.model_validate(
            {"type": "message", "source": {}, "message": {"type": "text", "text": "hi"}}
        )
        background_tasks = BackgroundTasks()
        await LineAdapter()._dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks == []

    async def test_missing_reply_token_passes_none(self) -> None:
        event = Event.model_validate(
            {
                "type": "message",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "hi"},
            }
        )
        background_tasks = BackgroundTasks()
        await LineAdapter()._dispatch_event(event, background_tasks, _settings())

        assert background_tasks.tasks[0].args[3] is None


# ---------------------------------------------------------------------------
# LineAdapter._dispatch_event — group + join routing
# ---------------------------------------------------------------------------


class TestGroupDispatch:
    async def _dispatch(self, event_dict: dict[str, object], settings: Settings) -> BackgroundTasks:
        """Dispatch one event with sender-name lookup stubbed, returning the tasks."""
        event = Event.model_validate(event_dict)
        background_tasks = BackgroundTasks()
        with patch(f"{_ADAPTER}.resolve_sender_name", new=AsyncMock(return_value="王小明")):
            await LineAdapter()._dispatch_event(event, background_tasks, settings)
        return background_tasks

    def _group_text(self, text: str, **message: object) -> dict[str, object]:
        return {
            "type": "message",
            "webhookEventId": "evt_g",
            "replyToken": "reply_g",
            "source": {"type": "group", "groupId": "C1", "userId": "U9"},
            "message": {"type": "text", "text": text, **message},
        }

    async def test_group_mention_is_addressed(self) -> None:
        mention = {"mention": {"mentionees": [{"index": 0, "length": 6, "isSelf": True}]}}
        background_tasks = await self._dispatch(
            self._group_text("@Alice 幫我排會議", **mention), _settings()
        )

        task = background_tasks.tasks[0]
        assert task.args[0] == "line_C1"
        assert task.kwargs["is_group"] is True
        assert task.kwargs["addressed"] is True
        assert task.kwargs["sender_id"] == "U9"
        assert task.kwargs["sender_name"] == "王小明"

    async def test_group_call_word_is_addressed(self) -> None:
        background_tasks = await self._dispatch(
            self._group_text("小幫手 幫我排會議"), _settings(GROUP_TRIGGER_PREFIXES="小幫手")
        )

        assert background_tasks.tasks[0].kwargs["addressed"] is True

    async def test_group_plain_message_is_not_addressed(self) -> None:
        background_tasks = await self._dispatch(self._group_text("早安"), _settings())

        task = background_tasks.tasks[0]
        assert task.kwargs["is_group"] is True
        assert task.kwargs["addressed"] is False

    async def test_group_sticker_is_group_but_not_addressed(self) -> None:
        event_dict = {
            "type": "message",
            "webhookEventId": "evt_s",
            "source": {"type": "group", "groupId": "C1", "userId": "U9"},
            "message": {"type": "sticker", "keywords": ["smile"]},
        }
        background_tasks = await self._dispatch(event_dict, _settings())

        task = background_tasks.tasks[0]
        assert task.kwargs["is_group"] is True
        assert task.kwargs["addressed"] is False

    async def test_direct_message_has_no_group_kwargs(self) -> None:
        """Regression: a 1:1 message schedules the exact same positional-only task as before."""
        event = Event.model_validate(
            {
                "type": "message",
                "webhookEventId": "evt_d",
                "replyToken": "reply_d",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "hi"},
            }
        )
        background_tasks = BackgroundTasks()
        await LineAdapter()._dispatch_event(event, background_tasks, _settings())

        task = background_tasks.tasks[0]
        assert task.args[0] == "line_U1"
        assert task.args[1] == "hi"
        assert task.args[3] == "reply_d"
        assert task.kwargs == {}

    async def test_join_event_schedules_greeting_reply(self) -> None:
        event = Event.model_validate(
            {
                "type": "join",
                "webhookEventId": "evt_j",
                "replyToken": "reply_j",
                "source": {"type": "group", "groupId": "C1"},
            }
        )
        background_tasks = BackgroundTasks()
        await LineAdapter()._dispatch_event(event, background_tasks, _settings())

        assert len(background_tasks.tasks) == 1
        task = background_tasks.tasks[0]
        # _deliver_reply(native_id, greeting, reply_token, config)
        assert task.args[0] == "C1"
        assert "小幫手" in task.args[1]
        assert task.args[2] == "reply_j"

    async def test_duplicate_join_event_is_skipped(self) -> None:
        event = Event.model_validate(
            {
                "type": "join",
                "webhookEventId": "evt_j_dup",
                "replyToken": "reply_j",
                "source": {"type": "group", "groupId": "C1"},
            }
        )
        adapter = LineAdapter()
        background_tasks = BackgroundTasks()
        await adapter._dispatch_event(event, background_tasks, _settings())
        await adapter._dispatch_event(event, background_tasks, _settings())

        assert len(background_tasks.tasks) == 1

    async def test_process_and_reply_forwards_group_fields_and_sends_nothing_when_empty(
        self,
    ) -> None:
        """An unaddressed group message (core returns []) sends nothing to LINE."""
        captured: dict[str, InboundMessage] = {}

        async def _fake_process(msg: InboundMessage, config: Settings) -> list[str]:
            captured["msg"] = msg
            return []

        with (
            patch(f"{_ADAPTER}.process_inbound", new=_fake_process),
            patch(f"{_ADAPTER}.push_line_message", new=AsyncMock()) as mock_push,
            patch(f"{_ADAPTER}.reply_line_message", new=AsyncMock()) as mock_reply,
        ):
            await LineAdapter()._process_and_reply(
                "line_C1",
                "早安",
                _settings(),
                None,
                is_group=True,
                addressed=False,
                sender_id="U9",
                sender_name="王小明",
            )

        msg = captured["msg"]
        assert msg.is_group is True
        assert msg.addressed is False
        assert msg.sender_id == "U9"
        assert msg.sender_name == "王小明"
        mock_push.assert_not_called()
        mock_reply.assert_not_called()


# ---------------------------------------------------------------------------
# LineAdapter._deliver_reply — reply-token-first, Push fallback
# ---------------------------------------------------------------------------


class TestDeliverReply:
    async def test_uses_reply_token_when_present(self) -> None:
        with (
            patch(f"{_ADAPTER}.reply_line_message", new=AsyncMock()) as mock_reply,
            patch(f"{_ADAPTER}.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await LineAdapter()._deliver_reply("room_A", "hello", "reply_token_1", _settings())

        mock_reply.assert_awaited_once_with("reply_token_1", "hello", TEST_TOKEN)
        mock_push.assert_not_called()

    async def test_falls_back_to_push_when_reply_token_rejected(self) -> None:
        with (
            patch(
                f"{_ADAPTER}.reply_line_message",
                new=AsyncMock(side_effect=ApiException(status=400)),
            ) as mock_reply,
            patch(f"{_ADAPTER}.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await LineAdapter()._deliver_reply("room_A", "hello", "expired_token", _settings())

        mock_reply.assert_awaited_once()
        mock_push.assert_awaited_once_with("room_A", "hello", TEST_TOKEN)

    async def test_pushes_directly_when_no_reply_token(self) -> None:
        with (
            patch(f"{_ADAPTER}.reply_line_message", new=AsyncMock()) as mock_reply,
            patch(f"{_ADAPTER}.push_line_message", new=AsyncMock()) as mock_push,
        ):
            await LineAdapter()._deliver_reply("room_A", "hello", None, _settings())

        mock_reply.assert_not_called()
        mock_push.assert_awaited_once_with("room_A", "hello", TEST_TOKEN)


# ---------------------------------------------------------------------------
# LineAdapter._process_and_reply — delivery wiring
# (orchestration itself lives in core.process_inbound)
# ---------------------------------------------------------------------------


async def test_process_and_reply_pushes_single_text_when_no_reply_token() -> None:
    """A single reply text with no reply token is delivered via Push."""
    with (
        patch(
            f"{_ADAPTER}.process_inbound",
            new=AsyncMock(return_value=["哈囉，我是 Hermes"]),
        ),
        patch(f"{_ADAPTER}.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await LineAdapter()._process_and_reply("line_room_AAA", "哈囉", _settings())

    # room_key comes in prefixed; the Push target is the stripped native id.
    mock_push.assert_awaited_once_with("room_AAA", "哈囉，我是 Hermes", TEST_TOKEN)


async def test_process_and_reply_uses_reply_token_for_first_text() -> None:
    """The first text uses the reply token; Push is not called for it."""
    with (
        patch(
            f"{_ADAPTER}.process_inbound",
            new=AsyncMock(return_value=["哈囉"]),
        ),
        patch(f"{_ADAPTER}.reply_line_message", new=AsyncMock()) as mock_reply,
        patch(f"{_ADAPTER}.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await LineAdapter()._process_and_reply(
            "line_room_AAA", "哈囉", _settings(), "reply_token_1"
        )

    mock_reply.assert_awaited_once()
    mock_push.assert_not_called()


async def test_process_and_reply_first_text_reply_token_rest_push() -> None:
    """With multiple texts, only the first uses the reply token; the rest are pushed."""
    with (
        patch(
            f"{_ADAPTER}.process_inbound",
            new=AsyncMock(return_value=["notice", "agent reply"]),
        ),
        patch(f"{_ADAPTER}.reply_line_message", new=AsyncMock()) as mock_reply,
        patch(f"{_ADAPTER}.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await LineAdapter()._process_and_reply(
            "line_room_AAA", "哈囉", _settings(), "reply_token_1"
        )

    mock_reply.assert_awaited_once_with("reply_token_1", "notice", TEST_TOKEN)
    mock_push.assert_awaited_once_with("room_AAA", "agent reply", TEST_TOKEN)


async def test_process_and_reply_delivers_nothing_on_empty_texts() -> None:
    """When core returns no texts (e.g. agent failure), nothing is sent to LINE."""
    with (
        patch(
            f"{_ADAPTER}.process_inbound",
            new=AsyncMock(return_value=[]),
        ),
        patch(f"{_ADAPTER}.reply_line_message", new=AsyncMock()) as mock_reply,
        patch(f"{_ADAPTER}.push_line_message", new=AsyncMock()) as mock_push,
    ):
        await LineAdapter()._process_and_reply(
            "line_room_AAA", "哈囉", _settings(), "reply_token_1"
        )

    mock_reply.assert_not_called()
    mock_push.assert_not_called()
