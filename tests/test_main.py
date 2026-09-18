from __future__ import annotations

from unittest.mock import AsyncMock, patch

from alice_office_router import google_oauth
from alice_office_router.main import app, lifespan

_MAIN = "alice_office_router.main"


async def test_lifespan_registers_and_clears_the_oauth_resume_hook() -> None:
    """Only a running app can resume: the hook lives exactly as long as one."""
    async with lifespan(app):
        assert google_oauth.on_authorized is not None

    assert google_oauth.on_authorized is None


async def test_the_registered_hook_resumes_the_members_parked_message() -> None:
    """google_oauth's (room_id, member_key) hook lands on core with settings."""
    with patch(f"{_MAIN}.resume_pending_auth", new=AsyncMock()) as mock_resume:
        async with lifespan(app):
            hook = google_oauth.on_authorized
            assert hook is not None
            await hook("line_U1", "line_u1")

    mock_resume.assert_awaited_once()
    room_key, member_key, config = mock_resume.await_args.args
    assert (room_key, member_key) == ("line_U1", "line_u1")
    assert config.LINE_CHANNEL_SECRET
