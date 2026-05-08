"""Telegram channel lifecycle and inbound dispatch wiring.

The outbound pipeline lives in :mod:`benchclaw.channels.telegrm.outbound`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from benchclaw.bus import MediaMetadata, MessageAddress, MessageBus, OutboundMessage, TypingEvent
from benchclaw.channels.base import BaseChannel
from benchclaw.channels.telegrm import outbound
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
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id

        # Use stable numeric ID, but keep username for allowlist compatibility
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        # Build content from text and/or media
        content_parts: list[str] = []
        media_paths: list[str] = []
        media_metadata: list[MediaMetadata] = []
        str_chat_id = str(chat_id)

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(f"caption: {message.caption}")

        # Handle media files
        media_file = None
        media_type: str | None = None

        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"

        # Download media if present
        if media_file and media_type and self._app:
            if not self.media_repo:
                logger.warning("Telegram received media but media_repo not configured; skipping")
            else:
                try:
                    file = await self._app.bot.get_file(media_file.file_id)
                    mime_type = getattr(media_file, "mime_type", None)
                    size_bytes = getattr(media_file, "file_size", None)
                    ext = self._get_extension(media_type, mime_type)

                    file_path = self.media_repo.register(
                        MessageAddress(self.name, str_chat_id),
                        sender_id=sender_id,
                        media_type=media_type,
                        ext=ext,
                        mime_type=mime_type,
                        timestamp=message.date,
                        original_name=getattr(media_file, "file_name", None),
                    )
                    await file.download_to_drive(str(file_path))
                    media_paths.append(self.media_repo.media_relpath(file_path))
                    media_metadata.append(
                        {
                            "path": str(file_path),
                            "media_type": media_type,
                            "mime_type": mime_type,
                            "size_bytes": size_bytes,
                            "saved_at": message.date.isoformat(timespec="seconds"),
                            "source_channel": self.name,
                            "original_name": getattr(media_file, "file_name", None),
                        }
                    )

                    logger.debug(f"Downloaded {media_type} to {file_path}")
                except Exception as e:
                    logger.error(f"Failed to download media: {e}")
                    media_metadata.append(
                        {
                            "path": None,
                            "media_type": media_type,
                            "mime_type": getattr(media_file, "mime_type", None),
                            "size_bytes": getattr(media_file, "file_size", None),
                            "saved_at": None,
                            "source_channel": self.name,
                            "original_name": getattr(media_file, "file_name", None),
                        }
                    )

        content = "\n".join(content_parts) if content_parts else "[empty message]"
        summon_source = self._detect_summon_source(message)
        message_metadata = {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "sender_label": user.first_name or user.username,
            "is_group": message.chat.type != "private",
        }
        if summon_source:
            message_metadata["summon"] = summon_source

        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")

        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            media_metadata=media_metadata,
            metadata=message_metadata,
            timestamp=message.date,
        )

    def _detect_summon_source(self, message: Any) -> str | None:
        reply_to_message = getattr(message, "reply_to_message", None)
        reply_author = getattr(reply_to_message, "from_user", None)
        if (
            self._bot_user_id is not None
            and reply_author is not None
            and getattr(reply_author, "id", None) == self._bot_user_id
        ):
            return "reply"

        username = self._bot_username
        if not username:
            return None
        mention_re = re.compile(rf"(?<!\w)@{re.escape(username)}\b", re.IGNORECASE)
        for maybe_text in (getattr(message, "text", None), getattr(message, "caption", None)):
            if isinstance(maybe_text, str) and mention_re.search(maybe_text):
                return "mention"
        return None

    async def notify_typing(self, event: TypingEvent) -> None:
        if event.is_typing:
            await self._typing.start(event.address.chat_id)
        else:
            self._typing.stop(event.address.chat_id)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        logger.error(f"Telegram error: {context.error}")

    def _get_extension(self, media_type: str | None, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if media_type is None:
            return ""

        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
