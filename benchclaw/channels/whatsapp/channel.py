"""WhatsApp channel lifecycle.

The inbound (bridge → bus) and outbound (bus → bridge) paths live in
:mod:`benchclaw.channels.whatsapp.inbound` and
:mod:`benchclaw.channels.whatsapp.outbound` respectively; this module
just owns the WebSocket connection and forwards events.
"""

from __future__ import annotations

import asyncio
import json

import websockets
from loguru import logger

from benchclaw.bus import MessageBus, OutboundMessage, TypingEvent
from benchclaw.channels.base import BaseChannel
from benchclaw.channels.whatsapp import inbound, outbound
from benchclaw.channels.whatsapp.address import WhatsAppId
from benchclaw.channels.whatsapp.config import WhatsAppConfig
from benchclaw.media import MediaRepository


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
                    if self.config.bridge_token:
                        await ws.send(
                            json.dumps({"type": "auth", "token": self.config.bridge_token})
                        )
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    async for message in ws:
                        try:
                            await inbound.handle_bridge_message(self, str(message))
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")

            except Exception as e:
                logger.warning(f"WhatsApp bridge connection error: {e}. Retrying...")
                await asyncio.sleep(5)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through the WhatsApp outbound pipeline."""
        await outbound.send(self, msg)

    async def notify_typing(self, event: TypingEvent) -> None:
        if not self._ws or not self._connected:
            return
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "typing",
                        "to": WhatsAppId.from_raw(event.address.chat_id).outbound_jid(),
                        "is_typing": event.is_typing,
                    }
                )
            )
        except Exception as e:
            logger.debug(f"Failed to send typing indicator: {e}")
