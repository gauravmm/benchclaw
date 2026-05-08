"""Outbound send pipeline.

Two concerns split across module functions, each invoked from :func:`send`:

* :func:`plan_segments` — content shape (media / text) → typed segments.
* :func:`dispatch` — segments → Telegram bot API calls.

Adding a new content shape means adding a dataclass in ``state.py`` and a
match arm in ``dispatch``; nothing else moves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filetype
from loguru import logger

from benchclaw.bus import OutboundMessage
from benchclaw.channels.telegrm.markdown_html import markdown_to_telegram_html, split_long
from benchclaw.channels.telegrm.state import MediaSegment, OutboundSegment, TextSegment

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel

# Telegram caps message text at 4096 chars and captions at 1024.
TELEGRAM_TEXT_LIMIT = 4096


async def send(channel: "TelegramChannel", msg: OutboundMessage) -> None:
    if not channel._app:
        logger.warning("Telegram bot not running")
        return
    try:
        chat_id = int(msg.address.chat_id)
    except ValueError:
        logger.error(f"Invalid chat_id: {msg.address.chat_id}")
        return

    segments = plan_segments(channel, msg)
    async for _ in dispatch(channel, chat_id, segments):
        pass


def plan_segments(channel: "TelegramChannel", msg: OutboundMessage) -> list[OutboundSegment]:
    if msg.media:
        media_path, mime = resolve_outbound_media(channel, msg)
        return [MediaSegment(path=media_path, mime=mime, caption=msg.content or None)]
    body = msg.content or ""
    return [TextSegment(body=body)] if body.strip() else []


def resolve_outbound_media(
    channel: "TelegramChannel", msg: OutboundMessage
) -> tuple[Path, str]:
    """Return (absolute_path, mime). Raises FileNotFoundError if missing.
    Uses media_repo when configured; otherwise probes via filetype."""
    ref = msg.media[0]
    if channel.media_repo and not Path(ref).is_absolute():
        return channel.media_repo.resolve_file(ref)
    media_path = Path(ref)
    if not media_path.is_absolute():
        media_path = Path.cwd() / media_path
    if not media_path.is_file():
        raise FileNotFoundError(f"Telegram media not found: {ref}")
    return media_path, filetype.guess_mime(str(media_path)) or ""


async def dispatch(
    channel: "TelegramChannel", chat_id: int, segments: list[OutboundSegment]
) -> AsyncIterator[int]:
    for seg in segments:
        match seg:
            case TextSegment(body=body):
                for piece in split_long(body.strip(), TELEGRAM_TEXT_LIMIT):
                    if not piece:
                        continue
                    sent = await post(channel, chat_id, piece, markdown=True)
                    if sent is not None:
                        yield sent
            case MediaSegment(path=path, mime=mime, caption=caption):
                sent = await post_media(channel, chat_id, path, mime, caption)
                if sent is not None:
                    yield sent


async def post(
    channel: "TelegramChannel",
    chat_id: int,
    body: str,
    *,
    markdown: bool = False,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send one Telegram text message; return the new message_id, or None on
    failure. Noops when the bot isn't running.

    ``markdown=True``: convert ``body`` from markdown to Telegram HTML and set
    ``parse_mode='HTML'``; on HTML parse error, retry with the raw body.
    """
    if not channel._app:
        return None
    kwargs: dict[str, Any] = {"chat_id": chat_id, "text": body}
    if markdown:
        kwargs["text"] = markdown_to_telegram_html(body)
        kwargs["parse_mode"] = "HTML"
    elif parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if kwargs.get("parse_mode") == "HTML":
        kwargs["disable_web_page_preview"] = True
    if reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id
        kwargs["allow_sending_without_reply"] = True
    try:
        sent = await channel._app.bot.send_message(**kwargs)
        return sent.message_id
    except Exception as e:
        if not markdown:
            logger.warning(f"Failed to send Telegram message: {e}")
            return None
        logger.warning(f"HTML parse failed, falling back to plain text: {e}")
        try:
            sent = await channel._app.bot.send_message(chat_id=chat_id, text=body)
            return sent.message_id
        except Exception as e2:
            logger.error(f"Error sending Telegram message: {e2}")
            return None


async def post_media(
    channel: "TelegramChannel",
    chat_id: int,
    path: Path,
    mime: str,
    caption: str | None,
) -> int | None:
    """Dispatch on MIME type so .mp4 and friends actually upload correctly."""
    assert channel._app
    kind = mime.split("/", 1)[0] if mime else ""
    html_caption = markdown_to_telegram_html(caption) if caption else None
    send_kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": html_caption,
        "parse_mode": "HTML" if html_caption else None,
    }
    try:
        with path.open("rb") as fh:
            if kind == "image":
                sent = await channel._app.bot.send_photo(photo=fh, **send_kwargs)
            elif kind == "video":
                sent = await channel._app.bot.send_video(video=fh, **send_kwargs)
            elif kind == "audio":
                sent = await channel._app.bot.send_audio(audio=fh, **send_kwargs)
            else:
                sent = await channel._app.bot.send_document(document=fh, **send_kwargs)
        return sent.message_id
    except Exception as e:
        logger.error(f"Error sending Telegram media: {e}")
        return None
