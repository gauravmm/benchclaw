"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from loguru import logger

from nanobot.bus import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Config


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels from config
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._stack = AsyncExitStack()

        self._init_channels()

    async def __aenter__(self) -> "ChannelManager":
        await self._stack.__aenter__()

        if not self.channels:
            logger.warning("No channels configured")
            return self

        # Why do we have this again? This is pretty bad design.
        dispatch_task = asyncio.create_task(self._dispatch_outbound(), name="dispatch")
        self._stack.callback(dispatch_task.cancel)

        for _, channel in self.channels.items():
            await self._stack.enter_async_context(channel)

        return self

    async def __aexit__(self, *exc_info) -> None:
        logger.info("Stopping all channels...")
        await self._stack.__aexit__(*exc_info)

    def _init_channels(self) -> None:
        """Initialize all configured channels."""
        from nanobot.channels.smtp_email import EmailChannel
        from nanobot.channels.telegram import TelegramChannel
        from nanobot.channels.whatsapp import WhatsAppChannel

        try:
            self.channels["telegram"] = TelegramChannel(self.config.channels.telegram, self.bus)
        except Exception as e:
            logger.warning(f"Telegram channel not available: {e}")

        try:
            self.channels["whatsapp"] = WhatsAppChannel(self.config.channels.whatsapp, self.bus)
        except Exception as e:
            logger.warning(f"WhatsApp channel not available: {e}")

        try:
            self.channels["email"] = EmailChannel(self.config.channels.email, self.bus)
        except Exception as e:
            logger.warning(f"Email channel not available: {e}")

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)

                channel = self.channels.get(msg.channel)
                if channel:
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        logger.error(f"Error sending to {msg.channel}: {e}")
                else:
                    logger.warning(f"Unknown channel: {msg.channel}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    def get_status(self) -> dict[str, tuple[bool, str]]:
        """Get status of all channels as {name: (is_running, description)}."""
        return {name: channel.status() for name, channel in self.channels.items()}
