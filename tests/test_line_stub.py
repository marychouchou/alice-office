from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Generator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from alice_office_router.channels.line.client import push_line_message
from alice_office_router.channels.line.profiles import resolve_sender_name

# scripts/ is not an importable package, so load the dev tool by file path
# (same pattern as tests/test_debug_room.py).
_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "line_stub.py"
_spec = importlib.util.spec_from_file_location("line_stub", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
line_stub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(line_stub)


# ---------------------------------------------------------------------------
# Routing — pure, no server needed
# ---------------------------------------------------------------------------


def test_reply_response_has_one_sent_message_per_message() -> None:
    """The SDK's response model requires 1-5 sentMessages entries."""
    body = {"messages": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}

    payload, matched = line_stub.resolve_response("POST", "/v2/bot/message/reply", body)

    assert matched is True
    assert len(payload["sentMessages"]) == 2


def test_profile_display_name_comes_from_the_user_id_tail() -> None:
    """A stub profile is recognizable per fake user without any real LINE data."""
    payload, matched = line_stub.resolve_response("GET", "/v2/bot/profile/Uxxxxab12", {})

    assert matched is True
    assert payload["displayName"] == "成員-ab12"
    assert payload["userId"] == "Uxxxxab12"


def test_group_member_profile_is_routed_like_a_profile() -> None:
    """Group-member lookups (channels/line/profiles.py) answer the same shape."""
    payload, matched = line_stub.resolve_response("GET", "/v2/bot/group/C123/member/Uzzzz9876", {})

    assert matched is True
    assert payload["displayName"] == "成員-9876"


def test_unknown_route_is_answered_empty_and_flagged() -> None:
    """An unmatched endpoint still gets a body, so the SDK never raises."""
    payload, matched = line_stub.resolve_response("POST", "/v2/bot/richmenu", {})

    assert payload == {}
    assert matched is False


def test_google_token_response_is_built_from_a_member_token_file(tmp_path: Path) -> None:
    """POST /token replays an existing member token in Google's wire shape."""
    member_file = tmp_path / "line_u_room.json"
    member_file.write_text(
        json.dumps(
            {
                "line_u_room": {
                    "access_token": "access-123",
                    "refresh_token": "refresh-123",
                    "expiry_date": 1,
                    "token_type": "Bearer",
                    "scope": "https://www.googleapis.com/auth/calendar",
                }
            }
        ),
        encoding="utf-8",
    )

    payload = line_stub.google_token_response(member_file)

    assert payload == {
        "access_token": "access-123",
        "refresh_token": "refresh-123",
        # The stored form is an absolute expiry_date; the router recomputes
        # one from expires_in, so the stub hands back a lifetime instead.
        "expires_in": line_stub.GOOGLE_TOKEN_EXPIRES_IN,
        "scope": "https://www.googleapis.com/auth/calendar",
        "token_type": "Bearer",
    }


def test_google_token_response_rejects_a_multi_account_file(tmp_path: Path) -> None:
    """A member file holds exactly one entry; anything else is not one."""
    member_file = tmp_path / "two.json"
    member_file.write_text(json.dumps({"a": {}, "b": {}}), encoding="utf-8")

    with pytest.raises(ValueError):
        line_stub.google_token_response(member_file)


# ---------------------------------------------------------------------------
# The running server — request logging and a real SDK round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_server(tmp_path: Path) -> Generator[tuple[str, Path], None, None]:
    """Run the stub on an ephemeral port for the duration of one test.

    Args:
        tmp_path: pytest's per-test temporary directory (holds the log file).

    Yields:
        (base URL of the running stub, path of its request log).
    """
    log_path = tmp_path / "requests.jsonl"
    server: ThreadingHTTPServer = line_stub.make_server(0, log_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", log_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _log_lines(log_path: Path) -> list[dict[str, object]]:
    """Read the stub's request log back.

    Args:
        log_path: The log file written by the stub.

    Returns:
        One parsed record per logged request, in order.
    """
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


async def test_pushed_reply_is_logged_and_accepted_by_the_sdk(
    stub_server: tuple[str, Path],
) -> None:
    """A real push through the SDK reaches the stub and lands in its log.

    This is the whole point of step 0b: with LINE_API_BASE_URL set, an e2e
    test can read what the router tried to say without a phone — and the
    stub's canned response must satisfy the SDK's own response model.
    """
    base_url, log_path = stub_server

    await push_line_message("room_AAA", "哈囉，我是 Hermes", "test_channel_token", base_url)

    records = _log_lines(log_path)
    assert len(records) == 1
    assert records[0]["path"] == "/v2/bot/message/push"
    assert records[0]["matched"] is True
    assert records[0]["texts"] == ["哈囉，我是 Hermes"]


async def test_group_member_lookup_gets_the_stub_profile(
    stub_server: tuple[str, Path],
) -> None:
    """Display-name resolution also honours the base URL, so groups work offline."""
    base_url, log_path = stub_server

    name = await resolve_sender_name("group", "C123", "Uzzzz9876", "test_channel_token", base_url)

    assert name == "成員-9876"
    assert _log_lines(log_path)[0]["path"] == "/v2/bot/group/C123/member/Uzzzz9876"
