"""Pydantic models for the WhatsApp bridge protocol."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from benchclaw.bus import MediaMetadata


class _BridgeModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BridgeMediaMetadata(_BridgeModel):
    path: str | None = None
    media_type: str = "file"
    mime_type: str | None = None
    size_bytes: int | None = None
    saved_at: str | None = None
    original_name: str | None = None

    def to_media_metadata(self, *, source_channel: str) -> MediaMetadata:
        return {
            "path": self.path,
            "media_type": self.media_type,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "saved_at": self.saved_at,
            "source_channel": source_channel,
            "original_name": self.original_name,
        }


class BridgeMessageEvent(_BridgeModel):
    type: Literal["message"]
    id: str
    chatId: str  # noqa: N815
    content: str
    timestamp: int | float | str | None = None
    isGroup: bool = False  # noqa: N815
    pushName: str | None = None  # noqa: N815
    senderName: str | None = None  # noqa: N815
    nameCache: dict[str, str] | None = None  # noqa: N815
    mediaMetadata: list[BridgeMediaMetadata] = Field(default_factory=list)  # noqa: N815
    mediaBase64: str | None = None  # noqa: N815
    mediaType: str | None = None  # noqa: N815
    mentions: list[str] | None = None  # noqa: N815
    replyTo: str | None = None  # noqa: N815
    botJids: list[str] | None = None  # noqa: N815


class BridgeStatusEvent(_BridgeModel):
    type: Literal["status"]
    status: str


class BridgeQrEvent(_BridgeModel):
    type: Literal["qr"]
    qr: str | None = None


class BridgeErrorEvent(_BridgeModel):
    type: Literal["error"]
    error: str | None = None


class BridgeSentEvent(_BridgeModel):
    type: Literal["sent"]


WhatsAppBridgeEvent = Annotated[
    BridgeMessageEvent | BridgeStatusEvent | BridgeQrEvent | BridgeErrorEvent | BridgeSentEvent,
    Field(discriminator="type"),
]
BRIDGE_EVENT_ADAPTER = TypeAdapter(WhatsAppBridgeEvent)
