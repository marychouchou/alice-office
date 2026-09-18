from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from alice_office_router.config import Settings
from alice_office_router.file_links import (
    FILE_LINK_INVALID_NOTICE,
    FILE_LINKS_DISABLED_NOTICE,
    _resolve_download,
    _substitute,
    publish_file_links,
)

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"

BASE_URL = "https://router.example.com"
# Must fullmatch the same room-key shape the API channel accepts, since the
# download route validates the URL's room segment against it.
ROOM = "line_U0123456789abcdef0123456789abcdef"
# A literal 43-character token (what secrets.token_urlsafe(32) produces),
# so the assertions can spell out the expected URL.
TOKEN = "Qm3fZ9xL-aB7cD1eF4gH6iJ8kL0mN2oP5qR7sT9uV1w"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Build a Settings instance rooted at tmp_path, allowing overrides.

    Args:
        tmp_path: Pytest tmp_path fixture, used as DATA_DIR/HOST_DATA_DIR.
        **overrides: Field overrides applied on top of the test defaults.

    Returns:
        A Settings instance suitable for unit tests.
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
    return Settings(**defaults)  # type: ignore[arg-type]


def _write_outbox(settings: Settings, token: str, name: str, content: bytes) -> Path:
    """Write one agent-side outbox entry, as the share_file tool would.

    Args:
        settings: The tmp-rooted Settings under test.
        token: The token the marker will carry.
        name: Filename inside the token directory.
        content: File bytes.

    Returns:
        The token directory that was created.
    """
    token_dir = settings.room_outbox_dir(ROOM) / token
    token_dir.mkdir(parents=True)
    (token_dir / name).write_bytes(content)
    return token_dir


def _write_published(settings: Settings, token: str, name: str, content: bytes) -> Path:
    """Write one already-published file, bypassing the publish step.

    Args:
        settings: The tmp-rooted Settings under test.
        token: The token the download URL will carry.
        name: Filename inside the token directory.
        content: File bytes.

    Returns:
        The published file's path.
    """
    token_dir = settings.room_published_dir(ROOM) / token
    token_dir.mkdir(parents=True)
    path = token_dir / name
    path.write_bytes(content)
    return path


@pytest.fixture
async def app_client(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, Settings]]:
    """Build an ASGI test client with get_settings overridden to a tmp-rooted Settings.

    Args:
        tmp_path: Pytest tmp_path fixture.

    Yields:
        Tuple of (AsyncClient, Settings) for use in route-level tests.
    """
    from alice_office_router.config import get_settings
    from alice_office_router.main import app

    settings = _settings(tmp_path)

    def _override() -> Settings:
        return settings

    app.dependency_overrides[get_settings] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, settings
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# _substitute — pure string rewriting, no filesystem
# ---------------------------------------------------------------------------


def test_substitute_replaces_a_marker_with_the_resolved_link() -> None:
    """A resolved token becomes whatever the resolver returned."""
    text = f"檔案好了：\noutbox://{TOKEN}"

    result = _substitute(text, lambda token: f"{BASE_URL}/files/{ROOM}/{token}")

    assert result == f"檔案好了：\n{BASE_URL}/files/{ROOM}/{TOKEN}"


def test_substitute_replaces_every_marker() -> None:
    """Two markers in one reply are both rewritten."""
    other = secrets.token_urlsafe(32)
    text = f"第一份 outbox://{TOKEN}\n第二份 outbox://{other}"

    result = _substitute(text, lambda token: f"L({token})")

    assert result == f"第一份 L({TOKEN})\n第二份 L({other})"


def test_substitute_leaves_text_without_markers_untouched() -> None:
    """The overwhelmingly common reply comes back byte-identical."""
    text = "今天的重點是三件事，第一……"

    assert _substitute(text, lambda _token: "SHOULD NOT BE USED") == text


@pytest.mark.parametrize("token", ["a" * 42, "a" * 44, "not-a-token"])
def test_substitute_ignores_wrong_length_tokens(token: str) -> None:
    """Only an exactly-43-character token is a marker, so no other text is eaten."""
    text = f"outbox://{token}"

    assert _substitute(text, lambda _token: "REPLACED") == text


def test_substitute_uses_the_invalid_notice_when_the_token_does_not_resolve() -> None:
    """An unresolvable token becomes a human sentence, never a raw placeholder."""
    text = f"檔案好了：outbox://{TOKEN}"

    result = _substitute(text, lambda _token: None)

    assert result == f"檔案好了：{FILE_LINK_INVALID_NOTICE}"


# ---------------------------------------------------------------------------
# publish_file_links
# ---------------------------------------------------------------------------


async def test_publish_replaces_markers_with_a_notice_when_disabled(tmp_path: Path) -> None:
    """Without PUBLIC_BASE_URL the user gets a sentence, and nothing is published."""
    settings = _settings(tmp_path, PUBLIC_BASE_URL="")
    _write_outbox(settings, TOKEN, "summary.md", b"hi")

    result = await publish_file_links(f"好了 outbox://{TOKEN}", ROOM, settings)

    assert result == f"好了 {FILE_LINKS_DISABLED_NOTICE}"
    assert not settings.published_files_dir.exists()


async def test_publish_copies_the_file_out_of_the_room_and_rewrites_the_marker(
    tmp_path: Path,
) -> None:
    """The happy path: published copy exists outside the room, outbox is cleared."""
    settings = _settings(tmp_path)
    outbox_dir = _write_outbox(settings, TOKEN, "摘要.md", b"# summary\n")

    result = await publish_file_links(f"做好了：\noutbox://{TOKEN}", ROOM, settings)

    assert result == f"做好了：\n{BASE_URL}/files/{ROOM}/{TOKEN}"
    published = settings.room_published_dir(ROOM) / TOKEN / "摘要.md"
    assert published.read_bytes() == b"# summary\n"
    # The published copy lives outside data/<room>/, which is the room's mount.
    assert not published.is_relative_to(settings.DATA_DIR / ROOM)
    assert not outbox_dir.exists()


async def test_publish_rejects_a_symlink_planted_in_the_outbox(tmp_path: Path) -> None:
    """A symlink pointing at another room's data is never followed or served."""
    settings = _settings(tmp_path)
    secret = tmp_path / "other_room" / "google" / "tokens.json"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"refresh_token": "s3cret"}', encoding="utf-8")
    token_dir = settings.room_outbox_dir(ROOM) / TOKEN
    token_dir.mkdir(parents=True)
    (token_dir / "tokens.json").symlink_to(secret)

    with patch("alice_office_router.file_links.struct_logger", new=Mock()) as mock_logger:
        result = await publish_file_links(f"給你 outbox://{TOKEN}", ROOM, settings)

    assert result == f"給你 {FILE_LINK_INVALID_NOTICE}"
    assert not (settings.room_published_dir(ROOM) / TOKEN).exists()
    mock_logger.warning.assert_called_once()
    event, kwargs = mock_logger.warning.call_args
    assert event == ("file_link_rejected",)
    # The token is the download credential, so only a prefix may be logged.
    assert kwargs["token_prefix"] == TOKEN[:8]
    assert TOKEN not in str(kwargs)


async def test_publish_rejects_a_file_over_the_size_cap(tmp_path: Path) -> None:
    """A file past FILE_LINK_MAX_BYTES is not published, however it got there."""
    settings = _settings(tmp_path, FILE_LINK_MAX_BYTES=16)
    _write_outbox(settings, TOKEN, "big.bin", b"x" * 17)

    with patch("alice_office_router.file_links.struct_logger", new=Mock()) as mock_logger:
        result = await publish_file_links(f"outbox://{TOKEN}", ROOM, settings)

    assert result == FILE_LINK_INVALID_NOTICE
    assert not (settings.room_published_dir(ROOM) / TOKEN).exists()
    assert mock_logger.warning.call_args[0] == ("file_link_rejected",)


async def test_publish_rejects_an_outbox_holding_more_than_one_entry(tmp_path: Path) -> None:
    """Exactly one file is the contract; anything else is ambiguous and refused."""
    settings = _settings(tmp_path)
    token_dir = _write_outbox(settings, TOKEN, "a.md", b"a")
    (token_dir / "b.md").write_bytes(b"b")

    result = await publish_file_links(f"outbox://{TOKEN}", ROOM, settings)

    assert result == FILE_LINK_INVALID_NOTICE


async def test_publish_is_idempotent_for_an_already_published_token(tmp_path: Path) -> None:
    """Re-pasting an old link reuses the published copy and keeps its original TTL."""
    settings = _settings(tmp_path)
    _write_outbox(settings, TOKEN, "summary.md", b"first")
    await publish_file_links(f"outbox://{TOKEN}", ROOM, settings)
    published = settings.room_published_dir(ROOM) / TOKEN / "summary.md"
    mtime = published.stat().st_mtime

    # The agent pastes the same link again a turn later; its outbox copy is
    # long gone, so only the idempotent path can serve this.
    result = await publish_file_links(f"再貼一次 outbox://{TOKEN}", ROOM, settings)

    assert result == f"再貼一次 {BASE_URL}/files/{ROOM}/{TOKEN}"
    assert published.read_bytes() == b"first"
    assert published.stat().st_mtime == mtime


async def test_publish_sweeps_this_rooms_expired_tokens(tmp_path: Path) -> None:
    """Cleanup rides on the next publish for the room — no scheduler, no branch."""
    settings = _settings(tmp_path, FILE_LINK_TTL_HOURS=1)
    stale_token = secrets.token_urlsafe(32)
    stale = _write_published(settings, stale_token, "old.md", b"old")
    long_ago = time.time() - 7200
    os.utime(stale, (long_ago, long_ago))
    os.utime(stale.parent, (long_ago, long_ago))
    _write_outbox(settings, TOKEN, "new.md", b"new")

    await publish_file_links(f"outbox://{TOKEN}", ROOM, settings)

    assert not stale.parent.exists()
    assert (settings.room_published_dir(ROOM) / TOKEN / "new.md").exists()


async def test_publish_does_not_touch_the_filesystem_without_a_marker(tmp_path: Path) -> None:
    """Every reply runs through here, so the no-marker case must stay free."""
    settings = _settings(tmp_path)

    result = await publish_file_links("只是普通的一句回覆", ROOM, settings)

    assert result == "只是普通的一句回覆"
    assert not settings.published_files_dir.exists()


# ---------------------------------------------------------------------------
# GET /files/{room_id}/{token}
# ---------------------------------------------------------------------------


async def test_download_serves_the_published_file_as_an_attachment(
    app_client: tuple[AsyncClient, Settings],
) -> None:
    """A valid link downloads the bytes, always as an attachment and never sniffed."""
    client, settings = app_client
    _write_published(settings, TOKEN, "report.pdf", b"%PDF-1.4 hi")

    response = await client.get(f"/files/{ROOM}/{TOKEN}")

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 hi"
    assert response.headers["content-disposition"] == 'attachment; filename="report.pdf"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/pdf"


async def test_download_answers_head_with_the_same_headers_and_no_body(
    app_client: tuple[AsyncClient, Settings],
) -> None:
    """A HEAD probe (download managers, link previews) gets the headers, not 405."""
    client, settings = app_client
    _write_published(settings, TOKEN, "report.pdf", b"%PDF-1.4 hi")

    response = await client.head(f"/files/{ROOM}/{TOKEN}")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "11"
    assert response.headers["content-disposition"] == 'attachment; filename="report.pdf"'
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_download_encodes_a_non_ascii_filename_in_the_header(
    app_client: tuple[AsyncClient, Settings],
) -> None:
    """Chinese filenames are the norm here; they ride the header, not the URL."""
    client, settings = app_client
    _write_published(settings, TOKEN, "合約摘要.md", b"# x")

    response = await client.get(f"/files/{ROOM}/{TOKEN}")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=utf-8''")
    assert "%E5%90%88%E7%B4%84" in disposition
    assert response.headers["content-type"].startswith("text/markdown")


@pytest.mark.parametrize("token", ["short", "a" * 44, "bad/token", "Qm3f$" + "a" * 38])
async def test_download_rejects_a_malformed_token(
    app_client: tuple[AsyncClient, Settings], token: str
) -> None:
    """A token that isn't 43 urlsafe characters never reaches the filesystem."""
    client, settings = app_client
    _write_published(settings, TOKEN, "report.pdf", b"x")

    response = await client.get(f"/files/{ROOM}/{token}")

    assert response.status_code == 404


@pytest.mark.parametrize("room", ["_files", "_google", "line_notahexid", "api_"])
async def test_download_rejects_a_malformed_room_id(
    app_client: tuple[AsyncClient, Settings], room: str
) -> None:
    """The room segment must be a real room key — `_files` and friends can't be one."""
    client, _settings_unused = app_client

    response = await client.get(f"/files/{room}/{TOKEN}")

    assert response.status_code == 404


def test_resolve_download_rejects_dot_dot_as_a_room_id(tmp_path: Path) -> None:
    """Checked below the route too, since an HTTP client normalizes `..` away."""
    settings = _settings(tmp_path)

    assert _resolve_download("..", TOKEN, settings) is None
    assert _resolve_download("../_google", TOKEN, settings) is None


async def test_download_rejects_an_expired_file(
    app_client: tuple[AsyncClient, Settings],
) -> None:
    """TTL is measured on the router's own copy, so touching it is not the agent's call."""
    client, settings = app_client
    path = _write_published(settings, TOKEN, "report.pdf", b"x")
    long_ago = time.time() - (settings.FILE_LINK_TTL_HOURS + 1) * 3600
    os.utime(path, (long_ago, long_ago))

    response = await client.get(f"/files/{ROOM}/{TOKEN}")

    assert response.status_code == 404


async def test_download_404s_for_an_unknown_token(
    app_client: tuple[AsyncClient, Settings],
) -> None:
    """Same 404, same detail as every other failure — no token oracle."""
    client, settings = app_client
    _write_published(settings, TOKEN, "report.pdf", b"x")
    other = secrets.token_urlsafe(32)

    known = await client.get(f"/files/{ROOM}/{TOKEN}")
    unknown = await client.get(f"/files/{ROOM}/{other}")

    assert known.status_code == 200
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Not found"}


async def test_download_rejects_a_symlink_inside_the_published_dir(
    app_client: tuple[AsyncClient, Settings], tmp_path: Path
) -> None:
    """Belt and braces: even under _files/, only a real regular file is served."""
    client, settings = app_client
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("s3cret", encoding="utf-8")
    token_dir = settings.room_published_dir(ROOM) / TOKEN
    token_dir.mkdir(parents=True)
    (token_dir / "secret.txt").symlink_to(secret)

    response = await client.get(f"/files/{ROOM}/{TOKEN}")

    assert response.status_code == 404


async def test_download_rejects_a_token_dir_holding_two_files(
    app_client: tuple[AsyncClient, Settings],
) -> None:
    """Ambiguity is a rejection, not a guess."""
    client, settings = app_client
    path = _write_published(settings, TOKEN, "a.md", b"a")
    (path.parent / "b.md").write_bytes(b"b")

    response = await client.get(f"/files/{ROOM}/{TOKEN}")

    assert response.status_code == 404


async def test_download_logs_nothing_containing_the_token(
    app_client: tuple[AsyncClient, Settings], caplog: pytest.LogCaptureFixture
) -> None:
    """The token is a credential: a 404 must not write it into the log stream."""
    client, _settings_unused = app_client

    with caplog.at_level(logging.DEBUG, logger="alice_office_router.file_links"):
        await client.get(f"/files/{ROOM}/{TOKEN}")

    assert TOKEN not in caplog.text
