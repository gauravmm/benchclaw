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
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filetype
from loguru import logger

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
        # Phase A keeps the legacy image-only constraint; Phase B drops it.
        if not mime or not mime.startswith("image/"):
            raise ValueError(f"WhatsApp outbound media is not an image: {msg.media[0]}")
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
                payload: dict[str, Any] = {"type": "send", "to": to}
                if caption:
                    payload["text"] = caption
                payload["imageBase64"] = base64.b64encode(path.read_bytes()).decode()
                payload["imageMimeType"] = mime
                await _send_payload(channel, payload)
                yield None


async def _send_payload(channel: "WhatsAppChannel", payload: dict[str, Any]) -> None:
    assert channel._ws is not None
    try:
        await channel._ws.send(json.dumps(payload))
    except Exception as e:
        logger.error(f"Error sending WhatsApp message: {e}")
