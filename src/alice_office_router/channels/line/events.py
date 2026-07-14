"""Typed models and inbound-text resolution for LINE webhook events.

This module owns the LINE wire format: it validates the raw webhook JSON into
pydantic models (only the fields this router actually reads are modeled) and
turns a single inbound message event into the text forwarded to a room's
Hermes agent. Per CLAUDE.md's routing table, all LINE message-format logic
lives in `channels/line/`; the adapter only orchestrates dispatch and delivery.

Validation is deliberately lenient about *values* but strict about *shape*:
unknown event/message type strings parse fine (LINE keeps adding new ones),
while a structurally malformed event is dropped from the batch (logged) so one
bad event never rejects the whole webhook.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from linebot.v3.messaging.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from alice_office_router.channels.line.client import download_line_content
from alice_office_router.config import Settings
from alice_office_router.container_manager import CONTAINER_DATA_DIR

logger = logging.getLogger(__name__)

_INCOMING_SUBDIR = "incoming"
# LINE message.type -> file extension used when caching downloaded media,
# mirroring Hermes Agent's own LINE adapter's choices. Its keys also enumerate
# the media message types handled by _download_and_note_media.
_MEDIA_EXTENSIONS = {"image": ".jpg", "audio": ".m4a", "video": ".mp4", "file": ".bin"}


class Message(BaseModel):
    """A LINE `message` object — only the fields this router reads are modeled."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    id: str | None = None
    text: str | None = None
    fileName: str | None = None
    keywords: list[str] | None = None
    title: str | None = None
    address: str | None = None


class Source(BaseModel):
    """A LINE event `source` object (user / group / room)."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    userId: str | None = None
    groupId: str | None = None
    roomId: str | None = None

    @property
    def room_id(self) -> str | None:
        """Resolve the room/user/group id matching this source's own type.

        Returns:
            The `<type>Id` value (e.g. `userId` for a user source), or None
            when the type is unknown/absent or that id field is empty.
        """
        if not self.type:
            return None
        resolved = getattr(self, f"{self.type}Id", None)
        return resolved if resolved else None


class Event(BaseModel):
    """A single LINE webhook event — only the fields this router reads."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    webhookEventId: str | None = None
    replyToken: str | None = None
    source: Source | None = None
    message: Message | None = None

    @property
    def room_id(self) -> str | None:
        """Resolve this event's room/user/group id from its source.

        Returns:
            The resolved room id, or None if the source is missing/unresolvable.
        """
        return self.source.room_id if self.source else None


class WebhookBody(BaseModel):
    """The top-level LINE webhook request body (only the events array is used)."""

    model_config = ConfigDict(extra="ignore")

    events: list[Event] = Field(default_factory=list)

    @field_validator("events", mode="before")
    @classmethod
    def _drop_malformed_events(cls, value: object) -> list[Event]:
        """Validate each event independently, skipping (and logging) bad ones.

        One structurally malformed event must never reject the whole webhook —
        LINE would then retry the entire batch, replaying the good events.

        Args:
            value: The raw `events` value from the request body.

        Returns:
            The successfully-parsed events, in order; malformed ones dropped.
        """
        if not isinstance(value, list):
            return []
        events: list[Event] = []
        for raw in value:
            try:
                events.append(Event.model_validate(raw))
            except ValidationError as exc:
                logger.warning(f"Skipping malformed LINE event ({exc.error_count()} error(s))")
        return events


def _resolve_media_filename(message: Message, msg_type: str, message_id: str) -> str:
    """Pick a filename for a downloaded media message.

    LINE's "file" message events carry the original upload's `fileName`
    (with its real extension), which we use as-is so the agent's own tools
    can recognize the file type (e.g. `.pdf`) instead of a generic `.bin`.
    Image/audio/video events don't carry an original filename, so those
    fall back to `<message_id><extension>`.

    Args:
        message: The LINE `message` object.
        msg_type: The LINE message type (image/audio/video/file).
        message_id: The LINE message id, used as a filename fallback.

    Returns:
        A filesystem-safe filename — any directory components in a
        supplied `fileName` are stripped to prevent path traversal.
    """
    file_name = message.fileName
    if file_name and file_name.strip():
        safe_name = Path(file_name).name
        if safe_name:
            return safe_name
    return f"{message_id}{_MEDIA_EXTENSIONS.get(msg_type, '.bin')}"


async def _download_and_note_media(message: Message, room_id: str, config: Settings) -> str | None:
    """Download a LINE media message's binary content into the room's shared volume.

    The room's Hermes agent container mounts the same directory at
    `CONTAINER_DATA_DIR`, so once written here the file is immediately visible
    to the agent's own file/vision/audio tools — we don't try to interpret the
    media ourselves.

    Args:
        message: The LINE `message` object (type in image/audio/video/file).
        room_id: Room identifier, used to resolve the shared volume path.
        config: Application settings (LINE token + data dir).

    Returns:
        A text notice telling the agent where to find the saved file (using
        the container-side mount path), or None if the download failed.
    """
    msg_type = message.type
    message_id = message.id
    if msg_type is None or not message_id:
        return None

    try:
        content = await download_line_content(message_id, config.LINE_CHANNEL_ACCESS_TOKEN)
    except ApiException as exc:
        logger.error(f"Failed to download LINE {msg_type} content {message_id}: {exc}")
        return None

    filename = _resolve_media_filename(message, msg_type, message_id)
    incoming_dir = config.DATA_DIR / room_id / _INCOMING_SUBDIR
    incoming_dir.mkdir(parents=True, exist_ok=True)
    (incoming_dir / filename).write_bytes(content)

    container_path = f"{CONTAINER_DATA_DIR}/{_INCOMING_SUBDIR}/{filename}"
    return (
        f"[使用者傳送了一個{msg_type}檔案，已存放於 {container_path}，"
        "請視需要用你的工具讀取並回覆。]"
    )


async def _handle_text(message: Message, room_id: str, config: Settings) -> str | None:
    """Return a text message's content, or None when it is blank."""
    text = message.text
    return text if text else None


async def _handle_sticker(message: Message, room_id: str, config: Settings) -> str | None:
    """Turn a sticker message into a short zh-TW placeholder for the agent."""
    keywords = message.keywords
    if keywords:
        return f"[使用者傳送了貼圖：{', '.join(keywords)}]"
    return "[使用者傳送了貼圖]"


async def _handle_location(message: Message, room_id: str, config: Settings) -> str | None:
    """Turn a location message into a short zh-TW placeholder for the agent."""
    title = message.title or ""
    address = message.address or ""
    return f"[使用者傳送了位置：{title} {address}]".strip()


# LINE message.type -> handler. Media types (image/audio/video/file) all share
# _download_and_note_media; every handler has the same signature so the table
# stays a flat dispatch (see CLAUDE.md Growth Discipline: dict dispatch table).
_MESSAGE_HANDLERS: dict[str, Callable[[Message, str, Settings], Awaitable[str | None]]] = {
    "text": _handle_text,
    "sticker": _handle_sticker,
    "location": _handle_location,
    **{media_type: _download_and_note_media for media_type in _MEDIA_EXTENSIONS},
}


async def resolve_inbound_text(event: Event, room_id: str, config: Settings) -> str | None:
    """Turn a single LINE message event into text to forward to the Hermes agent.

    Text messages pass through as-is. Media messages (image/audio/video/file)
    are downloaded and saved into the room's shared volume, then replaced with
    a notice telling the agent where to find the file. Stickers and locations
    become short placeholder text. Anything else is skipped.

    Args:
        event: A single LINE webhook "message" event.
        room_id: The resolved room_id, used for the shared volume path.
        config: Application settings.

    Returns:
        Text to forward to the Hermes agent, or None if nothing should be sent.
    """
    message = event.message
    if message is None or message.type is None:
        return None
    handler = _MESSAGE_HANDLERS.get(message.type)
    if handler is None:
        logger.info(f"Ignoring unsupported LINE message type: {message.type!r}")
        return None
    return await handler(message, room_id, config)
