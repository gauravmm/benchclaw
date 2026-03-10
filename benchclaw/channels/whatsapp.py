"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import base64
import json
from datetime import datetime

import websockets
from loguru import logger

from benchclaw.bus import MediaMetadata, MessageAddress, MessageBus, OutboundMessage
from benchclaw.channels.base import BaseChannel, ChannelConfig, register_channel
from benchclaw.utils import get_timestamped_media_dir


class WhatsAppConfig(ChannelConfig):
    """WhatsApp channel configuration."""

    summon: str = "mention_or_reply"  # Attention filter mode: always, mention, reply, mention_or_reply
    bridge_token: str = ""  # Shared token for bridge auth (optional, recommended)

    def make_channel(self, bus: MessageBus) -> "WhatsAppChannel":
        return WhatsAppChannel(self, bus)


register_channel("whatsapp", WhatsAppConfig)


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.

    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """

    name = "whatsapp"

    def __init__(self, config: WhatsAppConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
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
                try:
                    mime_type = data.get("mediaType", "image/jpeg")
                    ext_map = {
                        "image/jpeg": ".jpg",
                        "image/png": ".png",
                        "image/gif": ".gif",
                        "image/webp": ".webp",
                    }
                    ext = ext_map.get(mime_type, ".bin")
                    msg_id = str(data.get("id") or "")
                    filename_base = msg_id[:16] if msg_id else "media"
                    media_dir = get_timestamped_media_dir(
                        channel=self.name,
                        chat_id=sender,
                        timestamp=datetime.fromtimestamp(data["timestamp"])
                        if data.get("timestamp")
                        else None,
                    )
                    file_path = media_dir / f"{filename_base}{ext}"
                    file_path.write_bytes(base64.b64decode(data["mediaBase64"]))
                    media_paths.append(str(file_path))
                    # Update or add MediaMetadata entry for this image
                    saved_at = datetime.now().isoformat(timespec="seconds")
                    if media_metadata:
                        # Patch the first media entry with the saved path
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

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_paths or None,
                media_metadata=media_metadata,
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                    "_summon_source": data.get("summonSource"),
                    "first_name": data.get("pushName"),
                },
                occurred_at=(
                    datetime.fromtimestamp(data["timestamp"])
                    if isinstance(data.get("timestamp"), (int, float))
                    else None
                ),
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


if __name__ == "__main__":
    import sys

    from benchclaw.bus import MessageBus

    bridge_url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:3001"
    test_chat_id = sys.argv[2] if len(sys.argv) > 2 else None

    async def watch_connected(channel: WhatsAppChannel) -> None:
        """Send a message when the channel first becomes connected."""
        while True:
            await asyncio.sleep(0.5)
            if channel._connected:
                print("[test] Successfully connected to WhatsApp bridge!")
                if test_chat_id:
                    await channel.send(
                        OutboundMessage(
                            address=MessageAddress(
                                channel="whatsapp", chat_id="120363405977110775@g.us"
                            ),
                            content="Connected!",
                        )
                    )
                return

    async def print_inbound(bus: MessageBus, channel: WhatsAppChannel) -> None:
        while True:
            msg = await bus.consume_inbound()
            print(f"[inbound] {msg}")
            await channel.send(
                OutboundMessage(
                    address=MessageAddress(channel="whatsapp", chat_id=msg.chat_id),
                    content=msg.content[::-1],
                )
            )

    async def main() -> None:
        bus = MessageBus()
        config = WhatsAppConfig(bridge_url=bridge_url)
        channel = WhatsAppChannel(config, bus)

        print(f"[test] Connecting to bridge at {bridge_url} ...")
        await asyncio.gather(
            channel.background(),
            watch_connected(channel),
            print_inbound(bus, channel),
        )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
