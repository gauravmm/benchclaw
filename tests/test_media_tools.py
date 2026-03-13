from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.media import SearchImagesTool, SendImageTool
from benchclaw.bus import MessageAddress, MessageBus, OutboundMessage
from benchclaw.channels.telegrm import TelegramChannel, TelegramConfig
from benchclaw.channels.whatsapp import WhatsAppChannel, WhatsAppConfig
from benchclaw.media import MediaRepository

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_1X1)


@pytest.mark.asyncio
async def test_send_image_uses_current_address(tmp_path: Path):
    workspace = tmp_path
    image = workspace / "media" / "x.png"
    _write_png(image)
    bus = MessageBus()
    ctx = ToolContext(workspace=workspace, bus=bus, address=MessageAddress("telegram", "123"))

    result = await SendImageTool().execute(ctx, path="media/x.png", caption="hello")
    outbound = await bus.consume_outbound(channel="telegram")

    assert result == "Image sent to telegram:123"
    assert isinstance(outbound, OutboundMessage)
    assert outbound.address == MessageAddress("telegram", "123")
    assert outbound.media == ["media/x.png"]
    assert outbound.content == "hello"


@pytest.mark.asyncio
async def test_send_image_normalizes_whatsapp_shorthand_address(tmp_path: Path):
    workspace = tmp_path
    image = workspace / "media" / "x.png"
    _write_png(image)
    bus = MessageBus()
    ctx = ToolContext(
        workspace=workspace,
        bus=bus,
        address=MessageAddress("whatsapp", "222355137806442@lid"),
    )

    result = await SendImageTool().execute(
        ctx,
        path="media/x.png",
        caption="hello",
        address="whatsapp:222355137806442",
    )
    outbound = await bus.consume_outbound(channel="whatsapp")

    assert result == "Image sent to whatsapp:222355137806442"
    assert isinstance(outbound, OutboundMessage)
    assert outbound.address == MessageAddress("whatsapp", "222355137806442")


@pytest.mark.asyncio
async def test_send_image_rejects_non_image(tmp_path: Path):
    workspace = tmp_path
    bad = workspace / "notes.txt"
    bad.write_text("not an image", encoding="utf-8")
    ctx = ToolContext(
        workspace=workspace, bus=MessageBus(), address=MessageAddress("telegram", "1")
    )

    with pytest.raises(ValueError, match="not an image"):
        await SendImageTool().execute(ctx, path="notes.txt")


@pytest.mark.asyncio
async def test_search_images_defaults_to_global_caption_search(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    repo.load()
    path = repo.register(
        MessageAddress("telegram", "chat-1"),
        sender_id="alice",
        media_type="image",
        ext=".png",
        mime_type="image/png",
        timestamp=datetime(2026, 3, 10, 14, 23, 0),
        original_name="receipt.png",
    )
    _write_png(path)
    repo.set_caption(repo.media_relpath(path), "receipt from grocery store")
    generic = tmp_path / "images" / "diagram.png"
    _write_png(generic)
    repo.set_caption("images/diagram.png", "architecture diagram")

    ctx = ToolContext(
        workspace=tmp_path,
        media_repo=repo,
        address=MessageAddress("telegram", "chat-1"),
    )
    result = await SearchImagesTool().execute(ctx)
    parsed = json.loads(result)

    assert {item["path"] for item in parsed} == {repo.media_relpath(path), "images/diagram.png"}


@pytest.mark.asyncio
async def test_search_images_filters_explicit_address(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    repo.load()
    first = repo.register(
        MessageAddress("telegram", "chat-1"),
        sender_id="alice",
        media_type="image",
        ext=".png",
        mime_type="image/png",
        timestamp=datetime(2026, 3, 10, 14, 23, 0),
        original_name="receipt.png",
    )
    second = repo.register(
        MessageAddress("telegram", "chat-2"),
        sender_id="bob",
        media_type="image",
        ext=".png",
        mime_type="image/png",
        timestamp=datetime(2026, 3, 10, 14, 24, 0),
        original_name="receipt-2.png",
    )
    _write_png(first)
    _write_png(second)
    repo.set_caption(repo.media_relpath(first), "receipt one")
    repo.set_caption(repo.media_relpath(second), "receipt two")

    ctx = ToolContext(workspace=tmp_path, media_repo=repo)
    result = await SearchImagesTool().execute(
        ctx,
        query="receipt",
        address="telegram:chat-2",
    )
    parsed = json.loads(result)

    assert [item["address"] for item in parsed] == ["telegram:chat-2"]


@pytest.mark.asyncio
async def test_search_images_normalizes_whatsapp_shorthand_address(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    repo.load()
    path = repo.register(
        MessageAddress("whatsapp", "222355137806442@lid"),
        sender_id="alice",
        media_type="image",
        ext=".png",
        mime_type="image/png",
        timestamp=datetime(2026, 3, 10, 14, 23, 0),
        original_name="receipt.png",
    )
    _write_png(path)
    repo.set_caption(repo.media_relpath(path), "receipt")

    ctx = ToolContext(
        workspace=tmp_path,
        media_repo=repo,
        address=MessageAddress("whatsapp", "222355137806442@lid"),
    )
    result = await SearchImagesTool().execute(
        ctx,
        query="receipt",
        address="whatsapp:222355137806442",
    )
    parsed = json.loads(result)

    assert [item["address"] for item in parsed] == ["whatsapp:222355137806442"]


@pytest.mark.asyncio
async def test_search_images_matches_nested_whatsapp_lid_record(tmp_path: Path):
    nested = {
        "images": {
            "receipt.png": {
                "_entry": {
                    "address": "whatsapp:222355137806442@lid",
                    "sender_id": "alice",
                    "timestamp": "2026-03-10T14:23:00",
                    "media_type": "image",
                    "mime_type": "image/png",
                    "original_name": "receipt.png",
                    "caption": "receipt",
                }
            }
        }
    }
    (tmp_path / ".media.json").write_text(json.dumps(nested), encoding="utf-8")

    repo = MediaRepository(tmp_path)
    repo.load()
    ctx = ToolContext(workspace=tmp_path, media_repo=repo)
    result = await SearchImagesTool().execute(
        ctx,
        query="receipt",
        address="whatsapp:222355137806442",
    )
    parsed = json.loads(result)

    assert [item["address"] for item in parsed] == ["whatsapp:222355137806442"]


class _FakeTelegramBot:
    def __init__(self) -> None:
        self.sent_photo: dict | None = None
        self.sent_text: dict | None = None

    async def send_photo(self, **kwargs):
        self.sent_photo = kwargs

    async def send_message(self, **kwargs):
        self.sent_text = kwargs


@pytest.mark.asyncio
async def test_telegram_send_photo_uses_media(tmp_path: Path):
    image = tmp_path / "media" / "out.png"
    _write_png(image)
    channel = TelegramChannel(TelegramConfig(token="x"), MessageBus(), media_repo=None)
    bot = _FakeTelegramBot()
    channel._app = type("FakeApp", (), {"bot": bot})()

    await channel.send(
        OutboundMessage(
            address=MessageAddress("telegram", "123"),
            content="caption",
            media=[str(image)],
        )
    )

    assert bot.sent_photo is not None
    assert bot.sent_photo["chat_id"] == 123
    assert bot.sent_photo["caption"] == "caption"
    assert bot.sent_text is None


class _FakeWS:
    def __init__(self) -> None:
        self.payloads: list[str] = []

    async def send(self, payload: str) -> None:
        self.payloads.append(payload)


@pytest.mark.asyncio
async def test_whatsapp_send_serializes_image_payload(tmp_path: Path):
    image = tmp_path / "media" / "out.png"
    _write_png(image)
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_repo=None)
    channel._ws = _FakeWS()
    channel._connected = True

    await channel.send(
        OutboundMessage(
            address=MessageAddress("whatsapp", "123@s.whatsapp.net"),
            content="caption",
            media=[str(image)],
        )
    )

    [payload] = channel._ws.payloads
    parsed = json.loads(payload)
    assert parsed["type"] == "send"
    assert parsed["to"] == "123@s.whatsapp.net"
    assert parsed["text"] == "caption"
    assert parsed["imageMimeType"] == "image/png"
    assert isinstance(parsed["imageBase64"], str)


@pytest.mark.asyncio
async def test_whatsapp_send_normalizes_bare_chat_id(tmp_path: Path):
    image = tmp_path / "media" / "out.png"
    _write_png(image)
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_repo=None)
    channel._ws = _FakeWS()
    channel._connected = True

    await channel.send(
        OutboundMessage(
            address=MessageAddress("whatsapp", "123"),
            content="caption",
            media=[str(image)],
        )
    )

    [payload] = channel._ws.payloads
    parsed = json.loads(payload)
    assert parsed["to"] == "123@s.whatsapp.net"


@pytest.mark.asyncio
async def test_whatsapp_inbound_normalizes_direct_chat_id(tmp_path: Path):
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus, media_repo=None)
    payload = {
        "type": "message",
        "id": "m-direct",
        "chatId": "222355137806442@lid",
        "content": "hello",
        "timestamp": 1_700_000_060,
        "isGroup": False,
    }

    await channel._handle_bridge_message(json.dumps(payload))

    msg = await bus.consume_inbound(address=MessageAddress("whatsapp", "222355137806442"))
    assert msg.address == MessageAddress("whatsapp", "222355137806442")
