"""Inbound: Telegram updates → message bus translation.

The python-telegram-bot ``MessageHandler`` calls :func:`on_message` for
every text/photo/voice/audio/document update; that handler downloads
any attachment via the Telegram bot API, registers it with the
``MediaRepository``, and forwards the resulting :class:`InboundMessage`
through the channel's ``_handle_message``.

Pure refactor out of ``channel.py`` so the lifecycle file mirrors the
shape of ``channels/whatsapp/`` (channel + inbound + outbound).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from benchclaw.bus import MediaMetadata, MessageAddress

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


# Per-MIME extension lookup; bare ``media_type`` fallback below.
_MIME_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
}
_MEDIA_TYPE_DEFAULT_EXT = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}


def _get_extension(media_type: str | None, mime_type: str | None) -> str:
    """Pick a sensible file extension, MIME-first then media-type fallback."""
    if media_type is None:
        return ""
    if mime_type and mime_type in _MIME_EXT_MAP:
        return _MIME_EXT_MAP[mime_type]
    return _MEDIA_TYPE_DEFAULT_EXT.get(media_type, "")


async def on_message(
    channel: "TelegramChannel",
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle one inbound Telegram update (text + optional media)."""
    if not update.message or not update.effective_user:
        return

    message = update.message
    user = update.effective_user
    chat_id = message.chat_id

    # Use stable numeric ID, but keep username for allowlist compatibility
    sender_id = str(user.id)
    if user.username:
        sender_id = f"{sender_id}|{user.username}"

    channel._chat_ids[sender_id] = chat_id

    content_parts: list[str] = []
    media_paths: list[str] = []
    media_metadata: list[MediaMetadata] = []
    str_chat_id = str(chat_id)

    if message.text:
        content_parts.append(message.text)
    if message.caption:
        content_parts.append(f"caption: {message.caption}")

    media_file, media_type = _pick_media(message)
    if media_file and media_type and channel._app:
        await _save_media(
            channel,
            message,
            media_file,
            media_type,
            sender_id,
            str_chat_id,
            media_paths,
            media_metadata,
        )

    content = "\n".join(content_parts) if content_parts else "[empty message]"
    summon_source = _detect_summon_source(channel, message)
    message_metadata: dict[str, Any] = {
        "message_id": message.message_id,
        "user_id": user.id,
        "username": user.username,
        "sender_label": user.first_name or user.username,
        "is_group": message.chat.type != "private",
    }
    if summon_source:
        message_metadata["summon"] = summon_source

    logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")

    await channel._handle_message(
        sender_id=sender_id,
        chat_id=str_chat_id,
        content=content,
        media=media_paths,
        media_metadata=media_metadata,
        metadata=message_metadata,
        timestamp=message.date,
    )


def _pick_media(message: Any) -> tuple[Any | None, str | None]:
    """Return ``(file, media_type)`` for the first attachment kind that
    matches; ``(None, None)`` when the message is text-only."""
    if message.photo:
        return message.photo[-1], "image"  # largest photo
    if message.voice:
        return message.voice, "voice"
    if message.audio:
        return message.audio, "audio"
    if message.document:
        return message.document, "file"
    return None, None


async def _save_media(
    channel: "TelegramChannel",
    message: Any,
    media_file: Any,
    media_type: str,
    sender_id: str,
    chat_id: str,
    media_paths: list[str],
    media_metadata: list[MediaMetadata],
) -> None:
    if not channel.media_repo:
        logger.warning("Telegram received media but media_repo not configured; skipping")
        return
    try:
        file = await channel._app.bot.get_file(media_file.file_id)
        mime_type = getattr(media_file, "mime_type", None)
        size_bytes = getattr(media_file, "file_size", None)
        ext = _get_extension(media_type, mime_type)

        file_path = channel.media_repo.register(
            MessageAddress(channel.name, chat_id),
            sender_id=sender_id,
            media_type=media_type,
            ext=ext,
            mime_type=mime_type,
            timestamp=message.date,
            original_name=getattr(media_file, "file_name", None),
        )
        await file.download_to_drive(str(file_path))
        media_paths.append(channel.media_repo.media_relpath(file_path))
        media_metadata.append(
            {
                "path": str(file_path),
                "media_type": media_type,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "saved_at": message.date.isoformat(timespec="seconds"),
                "source_channel": channel.name,
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
                "source_channel": channel.name,
                "original_name": getattr(media_file, "file_name", None),
            }
        )


def _detect_summon_source(channel: "TelegramChannel", message: Any) -> str | None:
    """``"reply"`` if this message is a reply to one of the bot's own posts;
    ``"mention"`` if the bot's @username appears in the body or caption.
    Returns ``None`` otherwise."""
    reply_to_message = getattr(message, "reply_to_message", None)
    reply_author = getattr(reply_to_message, "from_user", None)
    if (
        channel._bot_user_id is not None
        and reply_author is not None
        and getattr(reply_author, "id", None) == channel._bot_user_id
    ):
        return "reply"

    username = channel._bot_username
    if not username:
        return None
    mention_re = re.compile(rf"(?<!\w)@{re.escape(username)}\b", re.IGNORECASE)
    for maybe_text in (getattr(message, "text", None), getattr(message, "caption", None)):
        if isinstance(maybe_text, str) and mention_re.search(maybe_text):
            return "mention"
    return None
