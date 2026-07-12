from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from alice_office_router.hermes_client import ask_hermes_agent


def _mock_response(status_code: int, json_body: dict[str, object]) -> MagicMock:
    """Build a mock httpx.Response with the given status and JSON body.

    Args:
        status_code: HTTP status code the mock response should report.
        json_body: Dict returned by the mock response's .json() method.

    Returns:
        A MagicMock standing in for an httpx.Response.
    """
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=response
        )
    else:
        response.raise_for_status.side_effect = None
    return response


async def test_ask_hermes_agent_returns_reply_content() -> None:
    """A successful chat completion response yields the assistant's text."""
    response = _mock_response(200, {"choices": [{"message": {"content": "哈囉，我是 Hermes"}}]})
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=response)):
        reply = await ask_hermes_agent(
            "http://hermes_room_AAA:8642", "room_AAA", "哈囉", "test_key"
        )

    assert reply == "哈囉，我是 Hermes"


async def test_ask_hermes_agent_sends_auth_and_session_headers() -> None:
    """The request carries the Bearer token and X-Hermes-Session-Id for continuity."""
    response = _mock_response(200, {"choices": [{"message": {"content": "ok"}}]})
    mock_post = AsyncMock(return_value=response)
    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        await ask_hermes_agent("http://hermes_room_AAA:8642", "room_AAA", "hi", "test_key")

    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer test_key"
    assert kwargs["headers"]["X-Hermes-Session-Id"] == "room_AAA"
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]


async def test_ask_hermes_agent_raises_on_missing_choices() -> None:
    """A response with no choices raises ValueError instead of returning empty text."""
    response = _mock_response(200, {"choices": []})
    with (
        patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=response)),
        pytest.raises(ValueError, match="no choices"),
    ):
        await ask_hermes_agent("http://hermes_room_AAA:8642", "room_AAA", "hi", "test_key")


async def test_ask_hermes_agent_raises_on_http_error() -> None:
    """A non-2xx response propagates as an httpx error."""
    response = _mock_response(500, {})
    with (
        patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=response)),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await ask_hermes_agent("http://hermes_room_AAA:8642", "room_AAA", "hi", "test_key")


async def test_ask_hermes_agent_raises_on_empty_message_content() -> None:
    """A choice whose message has blank content raises ValueError, not empty text."""
    response = _mock_response(200, {"choices": [{"message": {"content": ""}}]})
    with (
        patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=response)),
        pytest.raises(ValueError, match="no message content"),
    ):
        await ask_hermes_agent("http://hermes_room_AAA:8642", "room_AAA", "hi", "test_key")


async def test_ask_hermes_agent_ignores_unknown_response_fields() -> None:
    """Extra fields on the completion/choice/message must not break parsing."""
    response = _mock_response(
        200,
        {
            "id": "chatcmpl-1",
            "model": "hermes",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hi"},
                }
            ],
        },
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=response)):
        reply = await ask_hermes_agent("http://hermes_room_AAA:8642", "room_AAA", "hi", "test_key")

    assert reply == "hi"
