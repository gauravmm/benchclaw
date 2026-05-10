"""Outbound send pipeline for the WhatsApp channel.

Mirrors the shape of :mod:`benchclaw.channels.telegrm.outbound`:

* :func:`plan_segments` — content shape (media / text) → typed segments.
* :func:`dispatch` — segments → bridge JSON payloads.

Adding a new content shape means adding a dataclass in ``state.py`` and
a match arm in :func:`dispatch`; nothing else moves.

Phase A is a no-op refactor: outbound media is still image-only, the
same constraint the monolithic ``send`` enforced. Phase B grows the
MIME-aware dispatch and extends the bridge protocol.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filetype
from loguru import logger
from PIL import Image

from benchclaw.bus import OutboundMessage
from benchclaw.channels.whatsapp.address import WhatsAppId
from benchclaw.channels.whatsapp.state import MediaSegment, OutboundSegment, TextSegment

if TYPE_CHECKING:
    from benchclaw.channels.whatsapp.channel import WhatsAppChannel


async def send(channel: "WhatsAppChannel", msg: OutboundMessage) -> None:
    if not channel._ws or not channel._connected:
        logger.warning("WhatsApp bridge not connected")
        return

    try:
        segments = plan_segments(channel, msg)
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Error preparing WhatsApp send: {e}")
        return

    async for _ in dispatch(channel, msg, segments):
        pass


def plan_segments(
    channel: "WhatsAppChannel", msg: OutboundMessage
) -> list[OutboundSegment]:
    if msg.media:
        media_path, mime = resolve_outbound_media(channel, msg)
        return [MediaSegment(path=media_path, mime=mime, caption=msg.content or None)]
    body = msg.content or ""
    return [TextSegment(body=body)] if body else []


def resolve_outbound_media(
    channel: "WhatsAppChannel", msg: OutboundMessage
) -> tuple[Path, str]:
    """Return (absolute_path, mime). Raises FileNotFoundError if missing."""
    ref = msg.media[0]
    if channel.media_repo and not Path(ref).is_absolute():
        return channel.media_repo.resolve_file(ref)
    media_path = Path(ref)
    if not media_path.is_absolute():
        media_path = Path.cwd() / media_path
    if not media_path.is_file():
        raise FileNotFoundError(f"WhatsApp media not found: {ref}")
    return media_path, filetype.guess_mime(str(media_path)) or ""


async def dispatch(
    channel: "WhatsAppChannel", msg: OutboundMessage, segments: list[OutboundSegment]
) -> AsyncIterator[None]:
    """Send each segment over the bridge WebSocket."""
    to = WhatsAppId.from_raw(msg.address.chat_id).outbound_jid()
    for seg in segments:
        match seg:
            case TextSegment(body=body):
                await _send_payload(channel, {"type": "send", "to": to, "text": body})
                yield None
            case MediaSegment(path=path, mime=mime, caption=caption):
                async for _ in _dispatch_media(channel, to, path, mime, caption, msg):
                    yield None


async def _dispatch_media(
    channel: "WhatsAppChannel",
    to: str,
    path: Path,
    mime: str,
    caption: str | None,
    msg: OutboundMessage,
) -> AsyncIterator[None]:
    """Build the bridge payload for a single :class:`MediaSegment`, dispatching
    on MIME family. Audio doesn't support captions on WhatsApp natively,
    so a non-empty caption is sent as a follow-up text message after the
    audio (Phase C of WHATSAPP_PARITY)."""
    raw = path.read_bytes()
    if mime == "image/webp":
        raw, mime = _transcode_webp(raw)
    encoded = base64.b64encode(raw).decode()
    kind = mime.split("/", 1)[0] if mime else ""
    payload: dict[str, Any] = {"type": "send", "to": to}

    audio_caption_followup: str | None = None

    if kind == "image":
        payload["imageBase64"] = encoded
        payload["imageMimeType"] = mime
        if caption:
            payload["text"] = caption
    elif kind == "video":
        payload["videoBase64"] = encoded
        payload["videoMimeType"] = mime
        if caption:
            payload["text"] = caption
    elif kind == "audio":
        payload["audioBase64"] = encoded
        payload["audioMimeType"] = mime
        # WhatsApp drops audio captions; remember the caption so we can
        # send it as a follow-up message instead of silently losing it.
        audio_caption_followup = caption or None
    else:
        payload["documentBase64"] = encoded
        payload["documentMimeType"] = mime or "application/octet-stream"
        payload["documentName"] = path.name
        if caption:
            payload["text"] = caption

    await _send_payload(channel, payload)
    yield None

    if audio_caption_followup:
        await _send_payload(
            channel,
            {"type": "send", "to": to, "text": audio_caption_followup},
        )
        yield None


def _transcode_webp(raw: bytes) -> tuple[bytes, str]:
    # WhatsApp/Baileys does not render WebP through the image: field — it
    # routes there as a sticker. Re-encode to JPEG (opaque) or PNG (alpha)
    # so LLM-supplied WebP paths land as inline images.
    img = Image.open(BytesIO(raw))
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    out = BytesIO()
    if has_alpha:
        img.convert("RGBA").save(out, format="PNG")
        return out.getvalue(), "image/png"
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


async def _send_payload(channel: "WhatsAppChannel", payload: dict[str, Any]) -> None:
    assert channel._ws is not None
    try:
        await channel._ws.send(json.dumps(payload))
    except Exception as e:
        logger.error(f"Error sending WhatsApp message: {e}")
