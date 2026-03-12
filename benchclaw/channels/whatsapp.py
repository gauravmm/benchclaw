"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import filetype
import websockets
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from benchclaw.bus import MediaMetadata, MessageAddress, MessageBus, OutboundMessage, TypingEvent
from benchclaw.channels.base import BaseChannel, ChannelConfig, register_channel
from benchclaw.media import MediaRepository
from benchclaw.utils import parse_optional_timestamp


class _BridgeModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _BridgeMediaMetadata(_BridgeModel):
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


class _BridgeMessageEvent(_BridgeModel):
    type: Literal["message"]
    id: str
    chatId: str  # noqa: N815
    content: str
    timestamp: int | float | str | None = None
    isGroup: bool = False  # noqa: N815
    pushName: str | None = None  # noqa: N815
    senderName: str | None = None  # noqa: N815
    nameCache: dict[str, str] | None = None  # noqa: N815
    mediaMetadata: list[_BridgeMediaMetadata] = Field(default_factory=list)  # noqa: N815
    mediaBase64: str | None = None  # noqa: N815
    mediaType: str | None = None  # noqa: N815
    mentions: list[str] | None = None  # noqa: N815
    replyTo: str | None = None  # noqa: N815
    botJids: list[str] | None = None  # noqa: N815


class _BridgeStatusEvent(_BridgeModel):
    type: Literal["status"]
    status: str


class _BridgeQrEvent(_BridgeModel):
    type: Literal["qr"]
    qr: str | None = None


class _BridgeErrorEvent(_BridgeModel):
    type: Literal["error"]
    error: str | None = None


class _BridgeSentEvent(_BridgeModel):
    type: Literal["sent"]


WhatsAppBridgeEvent = Annotated[
    _BridgeMessageEvent
    | _BridgeStatusEvent
    | _BridgeQrEvent
    | _BridgeErrorEvent
    | _BridgeSentEvent,
    Field(discriminator="type"),
]
_BRIDGE_EVENT_ADAPTER = TypeAdapter(WhatsAppBridgeEvent)


class WhatsAppConfig(ChannelConfig):
    """WhatsApp channel configuration."""

    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (optional, recommended)

    def make_channel(
        self, bus: MessageBus, media_repo: MediaRepository | None = None
    ) -> "WhatsAppChannel":
        return WhatsAppChannel(self, bus, media_repo=media_repo)


register_channel("whatsapp", WhatsAppConfig)


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.

    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """

    name = "whatsapp"

    def __init__(
        self, config: WhatsAppConfig, bus: MessageBus, media_repo: MediaRepository | None = None
    ):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self.media_repo = media_repo
        self._ws = None
        self._connected = False

    def status(self) -> tuple[bool, str]:
        return (
            self._connected,
            f"bridge {'connected' if self._connected else 'disconnected'} ({self.config.bridge_url})",
        )

    async def background(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        logger.info(f"Connecting to WhatsApp bridge at {self.config.bridge_url}...")
        while True:
            try:
                async with websockets.connect(self.config.bridge_url) as ws:
                    self._ws = ws
                    # Send auth token if configured
                    if self.config.bridge_token:
                        await ws.send(
                            json.dumps({"type": "auth", "token": self.config.bridge_token})
                        )
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(str(message))
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")

            except Exception as e:
                logger.warning(f"WhatsApp bridge connection error: {e}. Retrying...")
                await asyncio.sleep(5)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            # TODO: Propagate a message back to the loop saying that WhatsApp is down.
            return

        try:
            payload = {"type": "send", "to": msg.chat_id, "text": msg.content}
            if msg.media:
                image_path = Path(msg.media[0])
                if not image_path.is_absolute():
                    base_dir = self.media_repo.media_dir.parent if self.media_repo else Path.cwd()
                    image_path = base_dir / image_path
                if not image_path.is_file():
                    raise FileNotFoundError(f"WhatsApp image not found: {msg.media[0]}")
                mime = filetype.guess_mime(str(image_path))
                if not mime or not mime.startswith("image/"):
                    raise ValueError(f"WhatsApp outbound media is not an image: {msg.media[0]}")
                payload["imageBase64"] = base64.b64encode(image_path.read_bytes()).decode()
                payload["imageMimeType"] = mime
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")

    async def notify_typing(self, event: TypingEvent) -> None:
        if not self._ws or not self._connected:
            return
        try:
            await self._ws.send(
                json.dumps(
                    {"type": "typing", "to": event.address.chat_id, "is_typing": event.is_typing}
                )
            )
        except Exception as e:
            logger.debug(f"Failed to send typing indicator: {e}")

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            event = _BRIDGE_EVENT_ADAPTER.validate_json(raw)
        except ValidationError as e:
            logger.warning(
                f"Invalid WhatsApp bridge payload: {e.errors()[0]['msg']}; raw={raw[:160]}"
            )
            return

        match event:
            case _BridgeMessageEvent():
                await self._handle_bridge_inbound(event)
            case _BridgeStatusEvent():
                logger.info(f"WhatsApp status: {event.status}")
                self._connected = event.status == "connected"
            case _BridgeQrEvent():
                logger.error("Scan QR code in the bridge terminal to connect WhatsApp")
            case _BridgeErrorEvent():
                logger.error(f"WhatsApp bridge error: {event.error}")
            case _BridgeSentEvent():
                pass  # Ignore the acknowledgement.

    async def _handle_bridge_inbound(self, event: _BridgeMessageEvent) -> None:
        chat_id = event.chatId
        sender_id = self._sender_id(chat_id)
        content = self._replace_mentions(event.content, event)
        source_ts = parse_optional_timestamp(event.timestamp)
        summon_source = self._detect_summon_source(event)

        media_metadata = [
            item.to_media_metadata(source_channel=self.name) for item in event.mediaMetadata
        ]
        media_paths = self._save_bridge_image(event, sender_id, source_ts, media_metadata)

        if content == "[Voice Message]":
            logger.info(
                f"Voice message received from {sender_id}, but direct download from bridge is not yet supported."
            )

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media_paths or None,
            media_metadata=media_metadata,
            metadata=self._message_metadata(event, sender_id, summon_source),
            timestamp=source_ts,
        )

    @staticmethod
    def canonical_jid(raw: str) -> str:
        text = raw.strip().lower()
        local, sep, domain = text.partition("@")
        local = local.split(":", 1)[0]
        if sep:
            return f"{local}@{domain}"
        return local

    def resolve_person_name(self, person_id: str, payload: _BridgeMessageEvent) -> str | None:
        value = (payload.nameCache or {}).get(self.canonical_jid(person_id))
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    def _bot_ids(self, payload: _BridgeMessageEvent) -> set[str]:
        return {self.canonical_jid(item) for item in payload.botJids or [] if item}

    def _replace_mentions(self, content: str, payload: _BridgeMessageEvent) -> str:
        for person_id in payload.mentions or []:
            name = (self.resolve_person_name(person_id, payload) or "").strip()
            localpart = self._mention_id(person_id)
            if name and localpart:
                replacement = name if name.startswith("@") else f"@{name}"
                content = re.sub(rf"(?<!\w)@{re.escape(localpart)}\b", replacement, content)
        return content

    def _detect_summon_source(
        self, payload: _BridgeMessageEvent
    ) -> Literal["mention", "reply"] | None:
        bot_jids = self._bot_ids(payload)
        if not payload.isGroup or not bot_jids:
            return None
        if payload.replyTo and self.canonical_jid(payload.replyTo) in bot_jids:
            return "reply"
        if any(self.canonical_jid(item) in bot_jids for item in payload.mentions or []):
            return "mention"
        return None

    @staticmethod
    def _sender_id(chat_id: str) -> str:
        return chat_id.split("@")[0] if "@" in chat_id else chat_id

    @staticmethod
    def _mention_id(person_id: str) -> str:
        return person_id.strip().lower().partition("@")[0].split(":", 1)[0]

    def _message_metadata(
        self,
        event: _BridgeMessageEvent,
        sender_id: str,
        summon_source: Literal["mention", "reply"] | None,
    ) -> dict[str, str | int | float | None | bool]:
        metadata: dict[str, str | int | float | None | bool] = {
            "message_id": event.id,
            "timestamp": event.timestamp,
            "is_group": event.isGroup,
            "sender_label": event.senderName or event.pushName or sender_id,
            "bot_name": next(
                (
                    resolved
                    for item in event.botJids or []
                    if (resolved := self.resolve_person_name(item, event))
                ),
                None,
            ),
        }
        if metadata["bot_name"] is None:
            metadata.pop("bot_name")
        if summon_source:
            metadata["_summon_source"] = summon_source
        return metadata

    def _save_bridge_image(
        self,
        event: _BridgeMessageEvent,
        sender_id: str,
        source_ts: datetime | None,
        media_metadata: list[MediaMetadata],
    ) -> list[str]:
        if not event.mediaBase64:
            return []
        if not self.media_repo:
            logger.warning("WhatsApp received image but media_repo not configured; skipping")
            return []

        try:
            mime_type = event.mediaType or "image/jpeg"
            ext = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(mime_type, ".bin")
            file_path = self.media_repo.register(
                MessageAddress(self.name, chat_id=event.chatId),
                sender_id=sender_id,
                media_type="image",
                ext=ext,
                mime_type=mime_type,
                timestamp=source_ts,
                original_name=media_metadata[0].get("original_name") if media_metadata else None,
            )
            file_path.write_bytes(base64.b64decode(event.mediaBase64))
            saved_at = datetime.now().isoformat(timespec="seconds")
            if media_metadata:
                media_metadata[0]["path"] = str(file_path)
                media_metadata[0]["saved_at"] = saved_at
            else:
                media_metadata.append(
                    {
                        "path": str(file_path),
                        "media_type": "image",
                        "mime_type": mime_type,
                        "size_bytes": None,
                        "saved_at": saved_at,
                        "source_channel": self.name,
                    }
                )
            logger.debug(f"Saved WhatsApp image to {file_path}")
            return [self.media_repo.media_relpath(file_path)]
        except Exception as e:
            logger.error(f"Failed to save WhatsApp image: {e}")
            return []
