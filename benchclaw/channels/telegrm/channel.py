"""Telegram channel lifecycle.

The inbound (``MessageHandler`` → bus) and outbound (bus → bot API)
paths live in :mod:`benchclaw.channels.telegrm.inbound` and
:mod:`benchclaw.channels.telegrm.outbound` respectively; this module
just owns the long-poll lifecycle and the typing manager.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from benchclaw.bus import MessageBus, OutboundMessage, TypingEvent
from benchclaw.channels.base import BaseChannel
from benchclaw.channels.telegrm import inbound, outbound
from benchclaw.channels.telegrm.config import TelegramConfig
from benchclaw.channels.telegrm.typing_loop import TypingManager
from benchclaw.media import MediaRepository


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.

    Simple and reliable - no webhook/public IP needed.
    """

    name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        media_repo: MediaRepository | None = None,
    ):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.media_repo = media_repo
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing = TypingManager()
        self._bot_username: str | None = None
        self._bot_user_id: int | None = None

    def status(self) -> tuple[bool, str]:
        if self._app:
            return (True, "connected")
        return (False, "not connected")

    async def background(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        # Build the application with larger connection pool to avoid pool-timeout on long runs
        req = HTTPXRequest(
            connection_pool_size=16, pool_timeout=5.0, connect_timeout=30.0, read_timeout=30.0
        )
        builder = (
            Application.builder().token(self.config.token).request(req).get_updates_request(req)
        )
        if self.config.proxy:
            builder = builder.proxy(self.config.proxy).get_updates_proxy(self.config.proxy)
        self._app = builder.build()
        self._typing.attach(self._app)
        self._app.add_error_handler(self._on_error)

        # Add message handler for text, photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                (
                    filters.TEXT
                    | filters.PHOTO
                    | filters.VOICE
                    | filters.AUDIO
                    | filters.Document.ALL
                )
                & ~filters.COMMAND,
                self._on_message,
            )
        )

        logger.info("Starting Telegram bot (polling mode)...")

        await self._app.initialize()
        await self._app.start()

        bot_info = await self._app.bot.get_me()
        self._bot_username = bot_info.username
        self._bot_user_id = bot_info.id
        logger.info(f"Telegram bot @{bot_info.username} connected")

        assert self._app.updater
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,
        )

        try:
            await asyncio.Future()  # Wait forever until CancelledError
        except asyncio.CancelledError:
            pass
        finally:
            self._typing.detach()
            if self._app:
                logger.info("Stopping Telegram bot...")
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
                self._app = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram via the outbound pipeline."""
        await outbound.send(self, msg)

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await inbound.on_message(self, update, context)

    async def notify_typing(self, event: TypingEvent) -> None:
        if event.is_typing:
            await self._typing.start(event.address.chat_id)
        else:
            self._typing.stop(event.address.chat_id)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        logger.error(f"Telegram error: {context.error}")
