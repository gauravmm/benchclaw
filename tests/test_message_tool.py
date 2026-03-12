from __future__ import annotations

import pytest

from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.message import MessageTool
from benchclaw.bus import MessageAddress, MessageBus, OutboundMessage


@pytest.mark.asyncio
async def test_message_tool_requires_explicit_target(tmp_path):
    bus = MessageBus()
    tool = MessageTool(send_callback=bus.publish_outbound)
    ctx = ToolContext(workspace=tmp_path, bus=bus, address=MessageAddress("telegram", "123"))

    with pytest.raises(ValueError, match="explicit channel and chat_id"):
        await tool.execute(ctx, content="hello")


@pytest.mark.asyncio
async def test_message_tool_rejects_current_conversation(tmp_path):
    bus = MessageBus()
    tool = MessageTool(send_callback=bus.publish_outbound)
    ctx = ToolContext(workspace=tmp_path, bus=bus, address=MessageAddress("telegram", "123"))

    with pytest.raises(ValueError, match="current conversation"):
        await tool.execute(ctx, content="hello", channel="telegram", chat_id="123")


@pytest.mark.asyncio
async def test_message_tool_sends_to_explicit_target(tmp_path):
    bus = MessageBus()
    tool = MessageTool(send_callback=bus.publish_outbound)
    ctx = ToolContext(workspace=tmp_path, bus=bus, address=MessageAddress("telegram", "123"))

    result = await tool.execute(ctx, content="hello", channel="whatsapp", chat_id="456")
    outbound = await bus.consume_outbound(channel="whatsapp")

    assert result == "Message sent to whatsapp:456"
    assert isinstance(outbound, OutboundMessage)
    assert outbound.address == MessageAddress("whatsapp", "456")
    assert outbound.content == "hello"
