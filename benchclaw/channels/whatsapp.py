"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import base64
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import websockets
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from benchclaw.bus import MediaMetadata, MessageAddress, MessageBus, OutboundMessage, TypingEvent
from benchclaw.channels.base import BaseChannel, ChannelConfig, register_channel
from benchclaw.media import MediaRepository


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
    mediaMetadata: list[_BridgeMediaMetadata] = Field(default_factory=list)  # noqa: N815
    mediaBase64: str | None = None  # noqa: N815
    mediaType: str | None = None  # noqa: N815
    mentionNames: dict[str, str] | None = None  # noqa: N815
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
    bot_name: str | None = None

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

        if isinstance(event, _BridgeMessageEvent):
            payload = event.model_dump(mode="python", exclude_none=True)
            chat_id = event.chatId
            content = self._replace_mentions(event.content, payload)
            source_ts = self._parse_source_timestamp(event.timestamp)
            summon_source = self._detect_summon_source(payload)
            if event.isGroup:
                logger.debug(
                    f"WhatsApp summon detection: chat_id={chat_id} data={payload} content={content!r} result={summon_source}"
                )
            media_metadata = [
                item.to_media_metadata(source_channel=self.name) for item in event.mediaMetadata
            ]

            # Download and save image if the bridge sent base64 data
            media_paths: list[str] = []
            if event.mediaBase64:
                if not self.media_repo:
                    logger.warning(
                        "WhatsApp received image but media_repo not configured; skipping"
                    )
                else:
                    try:
                        mime_type = event.mediaType or "image/jpeg"
                        ext_map = {
                            "image/jpeg": ".jpg",
                            "image/png": ".png",
                            "image/gif": ".gif",
                            "image/webp": ".webp",
                        }
                        ext = ext_map.get(mime_type, ".bin")
                        ts = source_ts
                        sender_id = chat_id.split("@")[0] if "@" in chat_id else chat_id
                        file_path = self.media_repo.register(
                            MessageAddress(self.name, sender_id), ext, mime_type, ts
                        )
                        file_path.write_bytes(base64.b64decode(event.mediaBase64))
                        media_paths.append(self.media_repo.media_relpath(file_path))
                        # Update or add MediaMetadata entry for this image
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
                    except Exception as e:
                        logger.error(f"Failed to save WhatsApp image: {e}")

            # Extract just the phone number or lid as chat_id
            sender_id = chat_id.split("@")[0] if "@" in chat_id else chat_id
            logger.info(f"Chat {chat_id}")

            # Handle voice transcription if it's a voice message
            if content == "[Voice Message]":
                logger.info(
                    f"Voice message received from {sender_id}, but direct download from bridge is not yet supported."
                )

            message_metadata = {
                "message_id": event.id,
                "timestamp": event.timestamp,
                "is_group": event.isGroup,
                "sender_label": event.senderName or event.pushName or sender_id,
            }
            if bot_name := self._bot_name(payload):
                message_metadata["bot_name"] = bot_name
            if summon_source:
                message_metadata["_summon_source"] = summon_source

            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,  # Use full LID for replies
                content=content,
                media=media_paths or None,
                media_metadata=media_metadata,
                metadata=message_metadata,
                timestamp=source_ts,
            )

        elif isinstance(event, _BridgeStatusEvent):
            # Connection status update
            logger.info(f"WhatsApp status: {event.status}")
            self._connected = event.status == "connected"

        elif isinstance(event, _BridgeQrEvent):
            logger.error("Scan QR code in the bridge terminal to connect WhatsApp")

        elif isinstance(event, _BridgeErrorEvent):
            logger.error(f"WhatsApp bridge error: {event.error}")

        elif isinstance(event, _BridgeSentEvent):
            pass  # Ignore the acknowledgement.

    @staticmethod
    def _parse_source_timestamp(raw: Any) -> datetime | None:
        ts_num: float | None = None
        if isinstance(raw, int | float):
            ts_num = float(raw)
        elif isinstance(raw, str):
            try:
                ts_num = float(raw)
            except ValueError:
                return None
        if ts_num is None:
            return None
        return datetime.fromtimestamp(ts_num, tz=timezone.utc)

    @staticmethod
    def _normalize_jid(raw: str) -> str:
        text = raw.strip().lower()
        local, sep, domain = text.partition("@")
        local = local.split(":", 1)[0]
        if sep:
            return f"{local}@{domain}"
        return local

    @staticmethod
    def _jid_localpart(raw: str) -> str:
        return raw.strip().lower().partition("@")[0].split(":", 1)[0]

    def _bot_name(self, payload: dict[str, Any]) -> str | None:
        config_name = self.config.bot_name
        if isinstance(config_name, str):
            config_name = config_name.strip()
            if config_name:
                return config_name
        return None

    def _replace_mentions(self, content: str, payload: dict[str, Any]) -> str:
        replacements: dict[str, str] = {}

        raw_mention_names = payload.get("mentionNames")
        if isinstance(raw_mention_names, dict):
            for jid, name in raw_mention_names.items():
                if not isinstance(jid, str) or not isinstance(name, str):
                    continue
                name = name.strip()
                if not name:
                    continue
                replacements[self._jid_localpart(jid)] = (
                    name if name.startswith("@") else f"@{name}"
                )

        if not replacements:
            return content

        updated = content
        for localpart, replacement in replacements.items():
            if not localpart:
                continue
            updated = re.sub(rf"(?<!\w)@{re.escape(localpart)}\b", replacement, updated)
        return updated

    def _detect_summon_source(self, payload: dict[str, Any]) -> Literal["mention", "reply"] | None:
        if not payload.get("isGroup"):
            return None

        raw_bot_jids = payload.get("botJids")
        bot_jids: set[str] = set()
        if isinstance(raw_bot_jids, list):
            for item in raw_bot_jids:
                if isinstance(item, str) and item:
                    bot_jids.add(self._normalize_jid(item))

        if not bot_jids:
            return None

        reply_raw = payload.get("replyTo")
        if isinstance(reply_raw, str) and self._normalize_jid(reply_raw) in bot_jids:
            return "reply"

        mention_names = payload.get("mentionNames")
        if isinstance(mention_names, dict):
            for item in mention_names:
                if isinstance(item, str) and self._normalize_jid(item) in bot_jids:
                    return "mention"
        return None
