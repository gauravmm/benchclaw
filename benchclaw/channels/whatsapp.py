"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import base64
import json
from datetime import datetime, timezone
from typing import Any, Literal

import websockets
from loguru import logger

from benchclaw.bus import MediaMetadata, MessageAddress, MessageBus, OutboundMessage, TypingEvent
from benchclaw.channels.base import BaseChannel, ChannelConfig, register_channel
from benchclaw.media import MediaRepository


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
        if self._connected:
            return (True, f"bridge connected ({self.config.bridge_url})")
        return (False, f"bridge disconnected ({self.config.bridge_url})")

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
        # asyncio.CancelledError falls through.

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
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from bridge: {raw[:100]}")
            return

        msg_type = data.get("type")
        if msg_type == "message":
            sender = str(data.get("sender", ""))
            content = str(data.get("content", ""))
            source_ts = self._parse_source_timestamp(data.get("timestamp"))
            summon_source = self._detect_summon_source(data)
            raw_media_metadata = data.get("media_metadata", [])
            media_metadata: list[MediaMetadata] = []
            if isinstance(raw_media_metadata, list):
                for item in raw_media_metadata:
                    if not isinstance(item, dict):
                        continue
                    media_type = str(item.get("media_type", "file"))
                    maybe_size = item.get("size_bytes")
                    size_bytes = maybe_size if isinstance(maybe_size, int) else None
                    media_metadata.append(
                        {
                            "path": str(item["path"])
                            if isinstance(item.get("path"), str)
                            else None,
                            "media_type": media_type,
                            "mime_type": (
                                str(item["mime_type"])
                                if isinstance(item.get("mime_type"), str)
                                else None
                            ),
                            "size_bytes": size_bytes,
                            "saved_at": (
                                str(item["saved_at"])
                                if isinstance(item.get("saved_at"), str)
                                else None
                            ),
                            "source_channel": self.name,
                            "original_name": (
                                str(item["original_name"])
                                if isinstance(item.get("original_name"), str)
                                else None
                            ),
                        }
                    )

            # Download and save image if the bridge sent base64 data
            media_paths: list[str] = []
            if data.get("mediaBase64"):
                if not self.media_repo:
                    logger.warning(
                        "WhatsApp received image but media_repo not configured; skipping"
                    )
                else:
                    try:
                        mime_type = data.get("mediaType", "image/jpeg")
                        ext_map = {
                            "image/jpeg": ".jpg",
                            "image/png": ".png",
                            "image/gif": ".gif",
                            "image/webp": ".webp",
                        }
                        ext = ext_map.get(mime_type, ".bin")
                        ts = source_ts
                        sender_id = sender.split("@")[0] if "@" in sender else sender
                        file_path = self.media_repo.register(
                            MessageAddress(self.name, sender_id), ext, mime_type, ts
                        )
                        file_path.write_bytes(base64.b64decode(data["mediaBase64"]))
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
            sender_id = sender.split("@")[0] if "@" in sender else sender
            logger.info(f"Sender {sender}")

            # Handle voice transcription if it's a voice message
            if content == "[Voice Message]":
                logger.info(
                    f"Voice message received from {sender_id}, but direct download from bridge is not yet supported."
                )

            message_metadata = {
                "message_id": data.get("id"),
                "timestamp": data.get("timestamp"),
                "is_group": data.get("isGroup", False),
                "sender_label": data.get("pushName") or sender_id,
            }
            if summon_source:
                message_metadata["_summon_source"] = summon_source

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_paths or None,
                media_metadata=media_metadata,
                metadata=message_metadata,
                timestamp=source_ts,
            )

        elif msg_type == "status":
            # Connection status update
            status = str(data.get("status"))
            logger.info(f"WhatsApp status: {status}")
            self._connected = status == "connected"

        elif msg_type == "qr":
            logger.error("Scan QR code in the bridge terminal to connect WhatsApp")

        elif msg_type == "error":
            logger.error(f"WhatsApp bridge error: {data.get('error')}")

        elif msg_type == "sent":
            pass  # Ignore the acknowledgement.

        else:
            logger.error(f"Unknown type: {msg_type}: " + str(data))

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

    def _detect_summon_source(self, payload: dict[str, Any]) -> Literal["mention", "reply"] | None:
        if not payload.get("isGroup"):
            return None

        bot_raw = payload.get("botJid")
        if not isinstance(bot_raw, str) or not bot_raw:
            return None
        bot_jid = self._normalize_jid(bot_raw)

        reply_raw = payload.get("replyTo")
        if isinstance(reply_raw, str) and self._normalize_jid(reply_raw) == bot_jid:
            return "reply"

        mentioned = payload.get("mentionedJids")
        if isinstance(mentioned, list):
            for item in mentioned:
                if isinstance(item, str) and self._normalize_jid(item) == bot_jid:
                    return "mention"
        return None
