from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# scripts/ is not an importable package, so load the dev tool by file path
# (same pattern as tests/test_debug_room.py). Only the pure helpers are
# exercised here — the committed suite must never require docker/LLM/network.
_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "e2e_smoke.py"
_spec = importlib.util.spec_from_file_location("e2e_smoke", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
e2e_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e_smoke)


# --- happy_path_ok ---------------------------------------------------------


def test_happy_path_ok_accepts_200_with_nonempty_replies() -> None:
    """A 200 carrying a non-blank reply is the only success shape."""
    assert e2e_smoke.happy_path_ok(200, {"replies": ["好"]}) is True


def test_happy_path_ok_rejects_non_200() -> None:
    """Any non-200 fails regardless of the body."""
    assert e2e_smoke.happy_path_ok(500, {"replies": ["好"]}) is False


def test_happy_path_ok_rejects_empty_replies_list() -> None:
    """An empty replies list means the agent produced nothing."""
    assert e2e_smoke.happy_path_ok(200, {"replies": []}) is False


def test_happy_path_ok_rejects_blank_only_replies() -> None:
    """Whitespace-only replies don't count as a real answer."""
    assert e2e_smoke.happy_path_ok(200, {"replies": ["   ", ""]}) is False


def test_happy_path_ok_rejects_non_dict_payload() -> None:
    """A raw-text (non-JSON) body can never pass."""
    assert e2e_smoke.happy_path_ok(200, "not json") is False


# --- cleanup targets (constants only, never user input) --------------------


def test_cleanup_container_names_api_only_by_default() -> None:
    """Without --line, only the API container is a removal target."""
    assert e2e_smoke.cleanup_container_names(include_line=False) == ["hermes_api_e2e"]


def test_cleanup_container_names_includes_line_when_requested() -> None:
    """--line adds exactly the synthetic LINE container."""
    names = e2e_smoke.cleanup_container_names(include_line=True)

    assert names == ["hermes_api_e2e", f"hermes_line_{'U' + 'f' * 32}"]


def test_cleanup_data_dirs_are_under_data_dir_and_named_by_room_key() -> None:
    """Data-dir targets are data_dir/<constant room key>, nothing else."""
    data_dir = Path("/srv/alice/data")

    dirs = e2e_smoke.cleanup_data_dirs(data_dir, include_line=True)

    assert dirs == [data_dir / "api_e2e", data_dir / f"line_{'U' + 'f' * 32}"]


def test_deletable_room_keys_match_cleanup_targets() -> None:
    """The remove_data_dir whitelist covers exactly the two throwaway rooms."""
    assert frozenset({"api_e2e", e2e_smoke.LINE_ROOM_KEY}) == e2e_smoke._DELETABLE_ROOM_KEYS


def test_line_container_name_derives_from_room_key() -> None:
    """The LINE container name is hermes_ + the prefixed room key."""
    assert e2e_smoke.LINE_CONTAINER == "hermes_line_" + "U" + "f" * 32


# --- resolve_data_dir ------------------------------------------------------


def test_resolve_data_dir_prefers_data_dir() -> None:
    """DATA_DIR (the router's own filesystem view) wins when set."""
    env = {"DATA_DIR": "/srv/alice/data", "HOST_DATA_DIR": "/other"}

    assert e2e_smoke.resolve_data_dir(env) == Path("/srv/alice/data")


def test_resolve_data_dir_falls_back_to_host_data_dir() -> None:
    """HOST_DATA_DIR is used when DATA_DIR is absent."""
    assert e2e_smoke.resolve_data_dir({"HOST_DATA_DIR": "/srv/x"}) == Path("/srv/x")


def test_resolve_data_dir_falls_back_to_repo_data() -> None:
    """An empty env falls back to the repo's own ./data."""
    result = e2e_smoke.resolve_data_dir({})

    assert result.name == "data"
    assert result.parent == Path(e2e_smoke.__file__).resolve().parent.parent


# --- build_line_webhook_body + sign ----------------------------------------


def test_build_line_webhook_body_uses_room_source_and_text() -> None:
    """The synthetic body carries the native id as a room source + the text."""
    raw = e2e_smoke.build_line_webhook_body("U" + "f" * 32, "哈囉")

    event = json.loads(raw)["events"][0]
    assert event["source"] == {"type": "room", "roomId": "U" + "f" * 32}
    assert event["message"] == {"type": "text", "text": "哈囉"}


def test_sign_matches_reference_hmac() -> None:
    """sign() reproduces LINE's base64(HMAC-SHA256(body, secret))."""
    import base64
    import hashlib
    import hmac

    body, secret = '{"events":[]}', "shhh"
    expected = base64.b64encode(
        hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    ).decode()

    assert e2e_smoke.sign(body, secret) == expected


# --- arg parsing -----------------------------------------------------------


def test_build_args_defaults() -> None:
    """Defaults: port 8899, neither --line nor --keep."""
    args = e2e_smoke.build_args([])

    assert args.port == 8899
    assert args.line is False
    assert args.keep is False


def test_build_args_flags_and_port() -> None:
    """Flags and a custom port parse as expected."""
    args = e2e_smoke.build_args(["--port", "8901", "--line", "--keep"])

    assert args.port == 8901
    assert args.line is True
    assert args.keep is True
