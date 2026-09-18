"""The first-party API channel: a trivial adapter for TUI / mobile / dev clients.

Unlike LINE, these are clients we control on both ends, so there is no
third-party wire format, signature, or reply token to honor (design §4.4). The
adapter exposes one bearer-authenticated `POST /webhooks/api/messages` that
funnels the request straight into `core.process_inbound` and returns the
agent's raw markdown replies synchronously — no LINE-style stripping/chunking,
because the canonical format is markdown and rendering belongs to each client.
Mounted only when `API_CHANNEL_TOKEN` is set (see channels.enabled_adapters).
"""

from __future__ import annotations

import hmac
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from structlog.contextvars import bound_contextvars

from alice_office_router.channels.base import InboundMessage
from alice_office_router.config import Settings, get_settings
from alice_office_router.conversation_log import record_turn
from alice_office_router.core import process_inbound

# room_key flows into a docker container name (hermes_<room_key>, whose network
# hostname must stay < 63 chars) and the Google account_key regex
# ^[a-z0-9_-]{1,64}$ (after lowercasing), so it can't be arbitrary text —
# path separators, spaces, or >64 chars would break a downstream identity.
# Accept only the two room shapes that exist today: an existing LINE room
# (line_ + native [UCR]+32 hex id) or this channel's own rooms (api_<slug>,
# 1-32 of [a-z0-9-]). A second real webhook channel (e.g. Telegram) adds its
# own shape here when it lands (design §4.3 / §4.4).
# Duplicated as file_links._ROOM_ID_RE, which validates the room segment of a
# download URL against the same two shapes — keep both in step; a third
# consumer is the signal to extract one shared pattern.
_ROOM_KEY_RE = re.compile(r"(?:line_[UCR][0-9a-f]{32}|api_[a-z0-9-]{1,32})")

logger = logging.getLogger(__name__)


class ApiInboundBody(BaseModel):
    """Request body for POST /webhooks/api/messages.

    Attributes:
        room_key: Target room key the core routes on. Either an existing
            `line_<native id>` room (curl into any room — the dev/debug entry
            point) or this channel's own `api_<slug>` room (design §4.4).
        text: The user message; must not be blank.
    """

    model_config = ConfigDict(extra="ignore")

    room_key: str
    text: str

    @field_validator("room_key")
    @classmethod
    def _validate_room_key(cls, value: str) -> str:
        """Reject any key that isn't a known-safe room shape.

        Args:
            value: The candidate room key.

        Returns:
            The value unchanged when it fully matches an accepted shape.

        Raises:
            ValueError: If the key is neither `line_<native id>` nor
                `api_<slug>` (surfaces to the client as HTTP 422).
        """
        if not _ROOM_KEY_RE.fullmatch(value):
            raise ValueError("room_key must be line_<native id> or api_<slug>")
        return value

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        """Reject blank (empty or whitespace-only) text.

        Args:
            value: The candidate message text.

        Returns:
            The value unchanged when it carries non-whitespace content.

        Raises:
            ValueError: If the text is blank (surfaces as HTTP 422).
        """
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


def _verify_bearer(authorization: str | None, expected: str | None) -> None:
    """Authorize a request by its `Authorization: Bearer <token>` header.

    Args:
        authorization: Raw Authorization header value, if present.
        expected: The configured API_CHANNEL_TOKEN to match against.

    Raises:
        HTTPException: 401 when the header is missing, malformed, or the token
            does not match (compared in constant time). The 401 carries no
            detail that could distinguish these cases.
    """
    provided = ""
    scheme, _, credential = (authorization or "").partition(" ")
    if scheme == "Bearer":
        provided = credential
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


class ApiChannelAdapter:
    """Channel adapter for the first-party API channel (TUI / mobile / dev).

    Mounted at `/webhooks/api`, it bearer-authenticates each request and hands
    it to the same channel-free pipeline every other adapter uses, returning
    the agent's raw replies in the HTTP response (design §4.4). It owns no wire
    format, dedup, or reply token — those are third-party concerns it doesn't
    have.
    """

    name: str = "api"

    def api_router(self) -> APIRouter:
        """Build the FastAPI router carrying the API-channel message endpoint.

        Returns:
            An APIRouter with a single POST route (`/messages`) so it resolves
            to `/webhooks/api/messages` under its mount prefix.
        """
        router = APIRouter()

        @router.post("/messages")
        async def api_messages(
            body: ApiInboundBody,
            settings: Annotated[Settings, Depends(get_settings)],
            authorization: Annotated[str | None, Header()] = None,
        ) -> dict[str, list[str]]:
            _verify_bearer(authorization, settings.API_CHANNEL_TOKEN)
            msg = InboundMessage(channel=self.name, room_key=body.room_key, text=body.text)
            # Marks every line of this turn as this channel's; core binds the
            # room_key itself (docs/logging-design.md §5.1).
            with bound_contextvars(channel=self.name):
                result = await process_inbound(msg, settings)
                # This channel's "delivery" is the HTTP response body being
                # built right here, so it cannot fail separately: True whenever
                # there is something to return, None when there is nothing.
                delivered = True if result.texts else None
                record_turn(result.envelope.model_copy(update={"delivered": delivered}), settings)
            return {"replies": result.texts}

        return router

    async def resume(self, msg: InboundMessage) -> None:
        """Drop a message parked for this channel: nothing here can push.

        Every other adapter answers a resume by pushing into the room it owns.
        This channel has no room and no connection to push over — its client
        asked over HTTP and that request was answered long ago — so a member
        who triggered an authorization link from a TUI/dev client simply asks
        again once they are done. Logged, never raised: core treats a dropped
        resume as a completed one.

        Args:
            msg: The parked inbound message, which is discarded.
        """
        logger.info(
            f"API channel cannot push; dropping the message parked for room "
            f"[{msg.room_key}] before Google authorization"
        )
