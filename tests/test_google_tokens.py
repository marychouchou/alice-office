"""Tests for the per-member Google token store and the tokens.json symlink swap."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings
from alice_office_router.google_tokens import (
    ANONYMOUS_MEMBER,
    account_key,
    check_member_token,
    load_member_tokens,
    member_key_for,
    migrate_legacy_tokens,
    save_member_tokens,
    select_member_tokens,
)

TEST_SECRET = "test_channel_secret"
TEST_TOKEN = "test_channel_access_token"
ROOM = "line_U_ROOM_ABC"
ROOM_KEY = "line_u_room_abc"

_FULL_SCOPES = (
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/gmail.modify "
    "https://www.googleapis.com/auth/drive"
)


def _settings(tmp_path: Path, *, google: bool = True, **overrides: object) -> Settings:
    """Build a Settings instance rooted at tmp_path.

    Args:
        tmp_path: Pytest tmp_path fixture, used as DATA_DIR/HOST_DATA_DIR.
        google: When True, make google_oauth_enabled True by setting a public
            URL and writing the deployment-level Web credentials seed file.
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
    }
    if google:
        defaults["PUBLIC_BASE_URL"] = "https://router.example.com"
    defaults.update(overrides)
    settings = Settings(**defaults)  # type: ignore[arg-type]
    if google:
        settings.google_web_creds_path.parent.mkdir(parents=True, exist_ok=True)
        settings.google_web_creds_path.write_text(
            json.dumps({"web": {"client_id": "id", "client_secret": "secret"}}), encoding="utf-8"
        )
    return settings


def _token_entry(
    *, scope: str = _FULL_SCOPES, refresh: str = "r", expires_in_ms: int = 3_600_000
) -> dict[str, object]:
    """Build one tokens-file entry, valid unless the caller says otherwise."""
    return {
        "access_token": "a",
        "refresh_token": refresh,
        "expiry_date": int(time.time() * 1000 + expires_in_ms),
        "token_type": "Bearer",
        "scope": scope,
    }


def _msg(**overrides: object) -> InboundMessage:
    """Build an InboundMessage with test defaults."""
    defaults: dict[str, object] = {"channel": "line", "room_key": ROOM, "text": "hi"}
    defaults.update(overrides)
    return InboundMessage(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# member_key_for
# ---------------------------------------------------------------------------


def test_member_key_for_direct_room_is_the_room_itself() -> None:
    """In a 1:1 room the member is the room, so the key is the room's account_key."""
    assert member_key_for(_msg()) == ROOM_KEY


def test_member_key_for_group_is_the_speaker() -> None:
    """In a group every turn runs as whoever spoke."""
    msg = _msg(is_group=True, sender_id="U_SENDER_1", sender_name="Amy")
    assert member_key_for(msg) == "u_sender_1"


def test_member_key_for_group_without_sender_id_is_none() -> None:
    """LINE withholds the userId of a non-friend, and that has no member key."""
    assert member_key_for(_msg(is_group=True, sender_id=None)) is None


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------


def test_load_member_tokens_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    """A member who never authorized normalizes to {} at the boundary, not an error."""
    settings = _settings(tmp_path)
    assert load_member_tokens(settings, ROOM, ROOM_KEY) == {}


def test_save_member_tokens_writes_world_readable_json_with_no_leftovers(tmp_path: Path) -> None:
    """The member file is 0644 (uid 10000 reads it) and no temp file survives."""
    settings = _settings(tmp_path)
    tokens = {ROOM_KEY: _token_entry()}

    save_member_tokens(settings, ROOM, ROOM_KEY, tokens)

    path = settings.room_google_member_tokens_path(ROOM, ROOM_KEY)
    assert json.loads(path.read_text(encoding="utf-8")) == tokens
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert [p.name for p in path.parent.iterdir()] == [f"{ROOM_KEY}.json"]


def test_save_member_tokens_replaces_in_place_and_round_trips(tmp_path: Path) -> None:
    """A second save overwrites the first, and load reads exactly what was written."""
    settings = _settings(tmp_path)
    save_member_tokens(settings, ROOM, ROOM_KEY, {ROOM_KEY: _token_entry(refresh="old")})
    save_member_tokens(settings, ROOM, ROOM_KEY, {ROOM_KEY: _token_entry(refresh="new")})

    loaded = load_member_tokens(settings, ROOM, ROOM_KEY)
    assert loaded[ROOM_KEY]["refresh_token"] == "new"


# ---------------------------------------------------------------------------
# migrate_legacy_tokens
# ---------------------------------------------------------------------------


def test_migrate_moves_a_regular_tokens_json_into_members(tmp_path: Path) -> None:
    """A pre-member-store tokens.json becomes members/<account_key(room)>.json."""
    settings = _settings(tmp_path)
    legacy = settings.room_google_tokens_path(ROOM)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({ROOM_KEY: _token_entry()}), encoding="utf-8")

    migrate_legacy_tokens(settings, ROOM)

    assert not legacy.exists()
    moved = settings.room_google_member_tokens_path(ROOM, ROOM_KEY)
    assert json.loads(moved.read_text(encoding="utf-8"))[ROOM_KEY]["access_token"] == "a"


def test_migrate_is_idempotent_and_never_touches_a_symlink(tmp_path: Path) -> None:
    """Running twice is a no-op, and an already-swapped tokens.json is left alone."""
    settings = _settings(tmp_path)
    legacy = settings.room_google_tokens_path(ROOM)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({ROOM_KEY: _token_entry()}), encoding="utf-8")

    migrate_legacy_tokens(settings, ROOM)
    select_member_tokens(settings, ROOM, ROOM_KEY)
    migrate_legacy_tokens(settings, ROOM)

    assert legacy.is_symlink()
    moved = settings.room_google_member_tokens_path(ROOM, ROOM_KEY)
    assert json.loads(moved.read_text(encoding="utf-8"))[ROOM_KEY]["access_token"] == "a"


def test_migrate_on_a_room_with_no_tokens_file_is_a_noop(tmp_path: Path) -> None:
    """Nothing to migrate must not create anything."""
    settings = _settings(tmp_path)
    migrate_legacy_tokens(settings, ROOM)
    assert not settings.room_google_members_dir(ROOM).exists()


# ---------------------------------------------------------------------------
# select_member_tokens
# ---------------------------------------------------------------------------


def test_select_creates_a_relative_symlink(tmp_path: Path) -> None:
    """tokens.json becomes a RELATIVE symlink; an absolute host path would dangle in the container."""
    settings = _settings(tmp_path)

    select_member_tokens(settings, ROOM, "u_sender_1")

    tokens_path = settings.room_google_tokens_path(ROOM)
    assert tokens_path.is_symlink()
    assert tokens_path.readlink() == Path("members/u_sender_1.json")
    assert not tokens_path.readlink().is_absolute()


def test_select_swaps_to_another_member(tmp_path: Path) -> None:
    """A second speaker repoints the same path at their own file, atomically."""
    settings = _settings(tmp_path)
    select_member_tokens(settings, ROOM, "u_sender_1")

    select_member_tokens(settings, ROOM, "u_sender_2")

    tokens_path = settings.room_google_tokens_path(ROOM)
    assert tokens_path.readlink() == Path("members/u_sender_2.json")
    # The temp symlink used for the swap must not survive it.
    leftovers = [p.name for p in tokens_path.parent.iterdir() if p.name.startswith(".tokens.json")]
    assert leftovers == []


def test_select_is_a_noop_when_already_pointing_at_that_member(tmp_path: Path) -> None:
    """The 1:1 common case must not replace the link on every turn."""
    settings = _settings(tmp_path)
    select_member_tokens(settings, ROOM, ROOM_KEY)
    tokens_path = settings.room_google_tokens_path(ROOM)
    before = tokens_path.lstat().st_ino

    select_member_tokens(settings, ROOM, ROOM_KEY)

    assert tokens_path.lstat().st_ino == before


def test_select_points_at_anonymous_for_an_unidentified_speaker(tmp_path: Path) -> None:
    """No member key means a target file that is never written, so Google simply fails."""
    settings = _settings(tmp_path)

    select_member_tokens(settings, ROOM, None)

    tokens_path = settings.room_google_tokens_path(ROOM)
    assert tokens_path.readlink() == Path(f"members/{ANONYMOUS_MEMBER}.json")
    assert not tokens_path.exists()  # dangling on purpose


def test_select_resolves_to_the_member_file_when_it_exists(tmp_path: Path) -> None:
    """The symlink really reaches the member's tokens through the room's google dir."""
    settings = _settings(tmp_path)
    save_member_tokens(settings, ROOM, "u_sender_1", {ROOM_KEY: _token_entry()})

    select_member_tokens(settings, ROOM, "u_sender_1")

    tokens_path = settings.room_google_tokens_path(ROOM)
    assert json.loads(tokens_path.read_text(encoding="utf-8"))[ROOM_KEY]["access_token"] == "a"


def test_select_migrates_a_legacy_room_on_the_first_turn(tmp_path: Path) -> None:
    """An upgraded 1:1 room keeps its authorization: the legacy file becomes its member file."""
    settings = _settings(tmp_path)
    legacy = settings.room_google_tokens_path(ROOM)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({ROOM_KEY: _token_entry()}), encoding="utf-8")

    select_member_tokens(settings, ROOM, ROOM_KEY)

    assert legacy.is_symlink()
    assert check_member_token(settings, ROOM, ROOM_KEY) == "ok"


def test_select_is_a_noop_when_google_oauth_is_disabled(tmp_path: Path) -> None:
    """A deployment without Google OAuth must not grow a google/ directory per room."""
    settings = _settings(tmp_path, google=False)

    assert select_member_tokens(settings, ROOM, ROOM_KEY) is False

    assert not settings.room_google_dir(ROOM).exists()


def test_select_reports_whether_the_link_actually_moved(tmp_path: Path) -> None:
    """The return value drives the after-swap work, so it must be exact, not "was called".

    True on the first turn (no link yet) and whenever the speaker changes;
    False for the 1:1 steady state, where repointing at the same target every
    turn would make container_manager.refresh_google_mount exec per message.
    """
    settings = _settings(tmp_path)

    assert select_member_tokens(settings, ROOM, "u_sender_1") is True
    assert select_member_tokens(settings, ROOM, "u_sender_1") is False
    assert select_member_tokens(settings, ROOM, "u_sender_2") is True
    assert select_member_tokens(settings, ROOM, None) is True
    assert select_member_tokens(settings, ROOM, None) is False


# ---------------------------------------------------------------------------
# check_member_token
# ---------------------------------------------------------------------------


def test_check_member_token_without_a_member_key_is_missing(tmp_path: Path) -> None:
    """An unidentified group speaker can never have a token."""
    settings = _settings(tmp_path)
    assert check_member_token(settings, ROOM, None) == "missing"


def test_check_member_token_without_a_file_is_missing(tmp_path: Path) -> None:
    """A member who never authorized reads as missing, not as an error."""
    settings = _settings(tmp_path)
    assert check_member_token(settings, ROOM, ROOM_KEY) == "missing"


def test_check_member_token_ignores_another_members_file(tmp_path: Path) -> None:
    """Authorization is per member: one member's token says nothing about another's."""
    settings = _settings(tmp_path)
    save_member_tokens(settings, ROOM, "u_sender_1", {ROOM_KEY: _token_entry()})

    assert check_member_token(settings, ROOM, "u_sender_2") == "missing"
    assert check_member_token(settings, ROOM, "u_sender_1") == "ok"


def test_check_member_token_partial_scopes_is_missing_scopes(tmp_path: Path) -> None:
    """A token granted before Drive was requested still works for calendar/gmail."""
    settings = _settings(tmp_path)
    save_member_tokens(
        settings,
        ROOM,
        ROOM_KEY,
        {
            ROOM_KEY: _token_entry(
                scope=(
                    "https://www.googleapis.com/auth/calendar "
                    "https://www.googleapis.com/auth/gmail.modify"
                )
            )
        },
    )

    assert check_member_token(settings, ROOM, ROOM_KEY) == "missing_scopes"


def test_check_member_token_expired_without_refresh_is_missing(tmp_path: Path) -> None:
    """An expired token with nothing to refresh it with is as good as no token."""
    settings = _settings(tmp_path)
    save_member_tokens(
        settings, ROOM, ROOM_KEY, {ROOM_KEY: _token_entry(refresh="", expires_in_ms=-3_600_000)}
    )

    assert check_member_token(settings, ROOM, ROOM_KEY) == "missing"


def test_check_member_token_expired_with_refresh_is_ok(tmp_path: Path) -> None:
    """The MCPs refresh for themselves, so an expired-but-refreshable token is fine."""
    settings = _settings(tmp_path)
    save_member_tokens(settings, ROOM, ROOM_KEY, {ROOM_KEY: _token_entry(expires_in_ms=-3_600_000)})

    assert check_member_token(settings, ROOM, ROOM_KEY) == "ok"


def test_check_member_token_malformed_file_is_missing(tmp_path: Path) -> None:
    """A corrupt member file is reported, not raised — the user just re-authorizes."""
    settings = _settings(tmp_path)
    path = settings.room_google_member_tokens_path(ROOM, ROOM_KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{{", encoding="utf-8")

    assert check_member_token(settings, ROOM, ROOM_KEY) == "missing"


def test_account_key_lowercases_ids() -> None:
    """Both room ids and sender ids go through the same lowercasing rule."""
    assert account_key("U196D1445F7FE156EAC44C02106F364EC") == "u196d1445f7fe156eac44c02106f364ec"


@pytest.mark.parametrize("member", ["u_a", "u_b"])
def test_select_then_check_reads_the_selected_member(tmp_path: Path, member: str) -> None:
    """The selected member is the one whose tokens the MCPs would see."""
    settings = _settings(tmp_path)
    save_member_tokens(settings, ROOM, member, {ROOM_KEY: _token_entry()})

    select_member_tokens(settings, ROOM, member)

    tokens_path = settings.room_google_tokens_path(ROOM)
    assert tokens_path.readlink() == Path(f"members/{member}.json")
    assert check_member_token(settings, ROOM, member) == "ok"
