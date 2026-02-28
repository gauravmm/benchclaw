"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import json

import websockets
from loguru import logger

from nanobot.bus import MessageAddress, MessageBus, OutboundMessage
from nanobot.channels.base import BaseChannel, ChannelConfig, register_channel


class WhatsAppConfig(ChannelConfig):
    """WhatsApp channel configuration."""

    bridge_url: str = "ws://localhost:3001"
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
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                },
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

        else:
            logger.error(f"Unknown type: {msg_type}: " + str(data))


if __name__ == "__main__":
    import sys

    from nanobot.bus import MessageBus

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
