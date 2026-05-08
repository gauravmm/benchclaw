"""Tests for WhatsApp's outbound MIME-based dispatch.

The MediaSegment dispatch in :mod:`benchclaw.channels.whatsapp.outbound`
dispatches on the first MIME family (image/video/audio/document) and
the bridge picks up whichever ``*Base64`` field is set. Audio doesn't
support captions natively, so a non-empty caption arrives as a
follow-up text message after the audio. These tests cover each branch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchclaw.bus import MessageAddress, MessageBus, OutboundMessage
from benchclaw.channels.whatsapp import WhatsAppChannel, WhatsAppConfig


class _CapturingWS:
    """Stand-in for the bridge WebSocket; records every JSON payload sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _channel() -> tuple[WhatsAppChannel, _CapturingWS]:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_repo=None)
    ws = _CapturingWS()
    channel._ws = ws  # type: ignore[assignment]
    channel._connected = True
    return channel, ws


def _write(path: Path, body: bytes = b"\x00\x01\x02") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.mark.asyncio
async def test_text_only_send_uses_text_branch(tmp_path: Path) -> None:
    channel, ws = _channel()
    msg = OutboundMessage(address=MessageAddress("whatsapp", "1@s.whatsapp.net"), content="hi")
    await channel.send(msg)
    assert len(ws.sent) == 1
    assert ws.sent[0]["text"] == "hi"
    assert "imageBase64" not in ws.sent[0]


@pytest.mark.asyncio
async def test_empty_outbound_sends_nothing(tmp_path: Path) -> None:
    """Empty content + no media ⇒ no payload (don't spam the bridge)."""
    channel, ws = _channel()
    msg = OutboundMessage(address=MessageAddress("whatsapp", "1@s.whatsapp.net"), content="")
    await channel.send(msg)
    assert ws.sent == []


@pytest.mark.asyncio
async def test_image_routes_through_imageBase64(tmp_path: Path) -> None:
    image = _write(tmp_path / "out.png", b"\x89PNG\r\n\x1a\nfake")
    channel, ws = _channel()
    msg = OutboundMessage(
        address=MessageAddress("whatsapp", "1@s.whatsapp.net"),
        content="caption text",
        media=[str(image)],
    )
    await channel.send(msg)
    assert len(ws.sent) == 1
    payload = ws.sent[0]
    assert "imageBase64" in payload
    assert payload["imageMimeType"].startswith("image/")
    assert payload["text"] == "caption text"
    assert "videoBase64" not in payload


@pytest.mark.asyncio
async def test_video_routes_through_videoBase64(tmp_path: Path) -> None:
    # Minimal MP4 box header so filetype.guess_mime returns video/mp4.
    mp4 = _write(
        tmp_path / "clip.mp4",
        bytes.fromhex("0000001866747970") + b"mp42" + b"\x00" * 32,
    )
    channel, ws = _channel()
    msg = OutboundMessage(
        address=MessageAddress("whatsapp", "1@s.whatsapp.net"),
        content="watch this",
        media=[str(mp4)],
    )
    await channel.send(msg)
    assert len(ws.sent) == 1
    payload = ws.sent[0]
    assert "videoBase64" in payload
    assert payload["videoMimeType"].startswith("video/")
    assert payload["text"] == "watch this"


@pytest.mark.asyncio
async def test_audio_routes_through_audioBase64_no_caption(tmp_path: Path) -> None:
    """Audio + empty caption ⇒ exactly one payload, no follow-up text."""
    # OGG magic so filetype.guess_mime returns audio/ogg.
    ogg = _write(tmp_path / "voice.ogg", b"OggS" + b"\x00" * 64)
    channel, ws = _channel()
    msg = OutboundMessage(
        address=MessageAddress("whatsapp", "1@s.whatsapp.net"),
        content="",
        media=[str(ogg)],
    )
    await channel.send(msg)
    assert len(ws.sent) == 1
    payload = ws.sent[0]
    assert "audioBase64" in payload
    assert payload["audioMimeType"].startswith("audio/")
    # WhatsApp doesn't support audio captions, and there isn't one anyway.
    assert "text" not in payload


@pytest.mark.asyncio
async def test_audio_with_caption_emits_followup_text(tmp_path: Path) -> None:
    """Audio + non-empty caption ⇒ audio payload then a separate text
    payload with the caption (WhatsApp drops audio captions natively)."""
    ogg = _write(tmp_path / "voice.ogg", b"OggS" + b"\x00" * 64)
    channel, ws = _channel()
    msg = OutboundMessage(
        address=MessageAddress("whatsapp", "1@s.whatsapp.net"),
        content="see the attached voice note",
        media=[str(ogg)],
    )
    await channel.send(msg)
    assert len(ws.sent) == 2
    # Audio comes first, with no text on it.
    assert "audioBase64" in ws.sent[0]
    assert "text" not in ws.sent[0]
    # Caption follows as a plain text message.
    assert ws.sent[1] == {
        "type": "send",
        "to": ws.sent[0]["to"],
        "text": "see the attached voice note",
    }


@pytest.mark.asyncio
async def test_document_routes_through_documentBase64_with_filename(
    tmp_path: Path,
) -> None:
    doc = _write(tmp_path / "report.pdf", b"%PDF-1.4 fake content")
    channel, ws = _channel()
    msg = OutboundMessage(
        address=MessageAddress("whatsapp", "1@s.whatsapp.net"),
        content="here",
        media=[str(doc)],
    )
    await channel.send(msg)
    assert len(ws.sent) == 1
    payload = ws.sent[0]
    assert "documentBase64" in payload
    assert payload["documentName"] == "report.pdf"
    # mime defaults sensibly when filetype can't guess it.
    assert payload["documentMimeType"]


@pytest.mark.asyncio
async def test_unknown_mime_falls_back_to_document(tmp_path: Path) -> None:
    """Files filetype can't recognise (e.g. plain .bin) take the document
    branch — better than dropping the send entirely."""
    blob = _write(tmp_path / "unknown.bin", b"\xde\xad\xbe\xef")
    channel, ws = _channel()
    msg = OutboundMessage(
        address=MessageAddress("whatsapp", "1@s.whatsapp.net"),
        content="",
        media=[str(blob)],
    )
    await channel.send(msg)
    assert len(ws.sent) == 1
    assert "documentBase64" in ws.sent[0]
    assert ws.sent[0]["documentMimeType"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_send_when_disconnected_skips_payload(tmp_path: Path) -> None:
    image = _write(tmp_path / "out.png", b"fake")
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_repo=None)
    channel._ws = None
    channel._connected = False
    msg = OutboundMessage(
        address=MessageAddress("whatsapp", "1@s.whatsapp.net"),
        content="never sent",
        media=[str(image)],
    )
    # Must not raise even though there's no transport.
    await channel.send(msg)


@pytest.mark.asyncio
async def test_missing_media_file_logs_and_returns(tmp_path: Path) -> None:
    """A media path that doesn't exist surfaces as a logged error rather
    than a crash. The bridge sees no payload."""
    channel, ws = _channel()
    msg = OutboundMessage(
        address=MessageAddress("whatsapp", "1@s.whatsapp.net"),
        content="caption",
        media=[str(tmp_path / "does_not_exist.png")],
    )
    await channel.send(msg)
    assert ws.sent == []
