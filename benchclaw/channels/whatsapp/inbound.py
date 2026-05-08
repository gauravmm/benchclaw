"""Inbound: bridge events → message bus translation.

The bridge sends one of five typed events (message / status / qr /
error / sent). :func:`handle_bridge_message` validates the JSON
payload, dispatches on event type, and feeds inbound user messages
through the channel's ``_handle_message`` so the agent loop sees them
as ordinary :class:`InboundMessage` records.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from loguru import logger
from pydantic import ValidationError

from benchclaw.bus import MediaMetadata, MessageAddress
from benchclaw.channels.whatsapp.bridge import (
    BRIDGE_EVENT_ADAPTER,
    BridgeErrorEvent,
    BridgeMessageEvent,
    BridgeQrEvent,
    BridgeSentEvent,
    BridgeStatusEvent,
)
from benchclaw.utils import now_aware, parse_optional_timestamp

if TYPE_CHECKING:
    from benchclaw.channels.whatsapp.channel import WhatsAppChannel


_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
}


async def handle_bridge_message(channel: "WhatsAppChannel", raw: str) -> None:
    """Dispatch one bridge JSON payload by event type."""
    try:
        event = BRIDGE_EVENT_ADAPTER.validate_json(raw)
    except ValidationError as e:
        logger.warning(
            f"Invalid WhatsApp bridge payload: {e.errors()[0]['msg']}; raw={raw[:160]}"
        )
        return

    match event:
        case BridgeMessageEvent():
            await _handle_inbound_message(channel, event)
        case BridgeStatusEvent():
            logger.info(f"WhatsApp status: {event.status}")
            channel._connected = event.status == "connected"
        case BridgeQrEvent():
            logger.error("Scan QR code in the bridge terminal to connect WhatsApp")
        case BridgeErrorEvent():
            logger.error(f"WhatsApp bridge error: {event.error}")
        case BridgeSentEvent():
            pass


async def _handle_inbound_message(
    channel: "WhatsAppChannel", event: BridgeMessageEvent
) -> None:
    chat_id = str(event.chatId)
    sender_id = event.chatId.localpart
    content = _replace_mentions(event.content, event)
    source_ts = parse_optional_timestamp(event.timestamp)
    summon_source = _detect_summon_source(event)

    media_metadata = [
        item.to_media_metadata(source_channel=channel.name) for item in event.mediaMetadata
    ]
    media_paths = _save_bridge_media(channel, event, sender_id, source_ts, media_metadata)

    await channel._handle_message(
        sender_id=sender_id,
        chat_id=chat_id,
        content=content,
        media=media_paths or None,
        media_metadata=media_metadata,
        metadata=_message_metadata(event, sender_id, summon_source),
        timestamp=source_ts,
    )


def _replace_mentions(content: str, payload: BridgeMessageEvent) -> str:
    """Rewrite ``@<jid_localpart>`` occurrences to ``@<display_name>`` using
    the bridge's ``nameCache``. Resolves mentions in-place so the agent
    sees readable names instead of opaque phone-number JIDs."""
    for person_id in payload.mentions or []:
        name = payload.resolve_name(person_id) or ""
        if name and person_id.localpart:
            replacement = name if name.startswith("@") else f"@{name}"
            content = re.sub(
                rf"(?<!\w)@{re.escape(person_id.localpart)}\b", replacement, content
            )
    return content


def _detect_summon_source(
    payload: BridgeMessageEvent,
) -> Literal["mention", "reply"] | None:
    """Group-only; returns "mention" / "reply" when the bot was addressed
    directly, or None for ambient group chatter."""
    bot_jids = set(payload.botJids or [])
    if not payload.isGroup or not bot_jids:
        return None
    if payload.replyTo and payload.replyTo in bot_jids:
        return "reply"
    if any(item in bot_jids for item in payload.mentions or []):
        return "mention"
    return None


def _message_metadata(
    event: BridgeMessageEvent,
    sender_id: str,
    summon_source: Literal["mention", "reply"] | None,
) -> dict[str, str | int | float | None | bool]:
    metadata: dict[str, str | int | float | None | bool] = {
        "message_id": event.id,
        "timestamp": event.timestamp,
        "is_group": event.isGroup,
        "sender_label": event.senderName or event.pushName or sender_id,
        "bot_name": next(
            (
                resolved
                for item in event.botJids or []
                if (resolved := event.resolve_name(item))
            ),
            None,
        ),
    }
    if metadata["bot_name"] is None:
        metadata.pop("bot_name")
    if summon_source:
        metadata["summon"] = summon_source
    return metadata


def _save_bridge_media(
    channel: "WhatsAppChannel",
    event: BridgeMessageEvent,
    sender_id: str,
    source_ts: datetime | None,
    media_metadata: list[MediaMetadata],
) -> list[str]:
    if not event.mediaBase64:
        return []
    if not channel.media_repo:
        logger.warning("WhatsApp received media but media_repo not configured; skipping")
        return []

    try:
        mime_type = event.mediaType or "application/octet-stream"
        ext = _MIME_EXT.get(mime_type, ".bin")
        # Derive broad media_type from the MIME prefix, falling back to the
        # existing metadata value so voice/audio distinction is preserved.
        mime_prefix = mime_type.split("/")[0]
        if mime_prefix in {"image", "audio", "video"}:
            media_type = mime_prefix
        elif media_metadata:
            media_type = media_metadata[0].get("media_type") or "file"
        else:
            media_type = "file"
        file_path = channel.media_repo.register(
            MessageAddress("whatsapp", event.chatId.comparable_id),
            sender_id=sender_id,
            media_type=media_type,
            ext=ext,
            mime_type=mime_type,
            timestamp=source_ts,
            original_name=media_metadata[0].get("original_name") if media_metadata else None,
        )
        Path(file_path).write_bytes(base64.b64decode(event.mediaBase64))
        saved_at = now_aware().isoformat(timespec="seconds")
        if media_metadata:
            media_metadata[0]["path"] = str(file_path)
            media_metadata[0]["saved_at"] = saved_at
        else:
            media_metadata.append(
                {
                    "path": str(file_path),
                    "media_type": media_type,
                    "mime_type": mime_type,
                    "size_bytes": None,
                    "saved_at": saved_at,
                    "source_channel": channel.name,
                }
            )
        logger.debug(f"Saved WhatsApp media to {file_path}")
        return [channel.media_repo.media_relpath(file_path)]
    except Exception as e:
        logger.error(f"Failed to save WhatsApp media: {e}")
        return []
