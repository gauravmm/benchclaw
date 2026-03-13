from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchclaw.agent.loop import AgentLoop, ToolCallTracker
from benchclaw.agent.tools.base import ToolContext
from benchclaw.bus import MessageAddress, MessageBus, OutboundMessage
from benchclaw.config import Config
from benchclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from benchclaw.session import Session


class _FakeProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        return self._response


def _make_loop(tmp_path: Path, response: LLMResponse) -> AgentLoop:
    config = Config()
    config.agents.master.workspace = str(tmp_path)
    return AgentLoop(config=config, bus=MessageBus(), provider=_FakeProvider(response))


@pytest.mark.asyncio
async def test_process_llm_turn_sends_visible_response(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, LLMResponse(content="Status update for you."))
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.append_user("What is the order status?")
    tracker = ToolCallTracker()

    async with loop.tools:
        call_ctx = ToolContext(
            workspace=loop.tools._master_ctx.workspace,
            bus=loop.bus,
            log_store=loop.tools._master_ctx.log_store,
            media_repo=loop.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        elapsed = await loop._process_llm_turn(
            session=session,
            tracker=tracker,
            call_ctx=call_ctx,
            addr=addr,
        )
        outbound = await loop.bus.consume_outbound(channel="telegram")

    assert elapsed >= 0
    assert isinstance(outbound, OutboundMessage)
    assert outbound.content == "Status update for you."
    assert session.events[-1].kind == "assistant"
    assert session.events[-1].content == "Status update for you."


@pytest.mark.asyncio
async def test_process_llm_turn_records_tool_calls_as_events(tmp_path: Path) -> None:
    loop = _make_loop(
        tmp_path,
        LLMResponse(
            content="Checking that now.",
            tool_calls=[
                ToolCallRequest(
                    id="tc1", name="log", arguments={"action": "append", "content": "step"}
                )
            ],
        ),
    )
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.append_user("Do the thing")
    tracker = ToolCallTracker()

    async with loop.tools:
        call_ctx = ToolContext(
            workspace=loop.tools._master_ctx.workspace,
            bus=loop.bus,
            log_store=loop.tools._master_ctx.log_store,
            media_repo=loop.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        await loop._process_llm_turn(
            session=session,
            tracker=tracker,
            call_ctx=call_ctx,
            addr=addr,
        )
        outbound = await loop.bus.consume_outbound(channel="telegram")

    assert isinstance(outbound, OutboundMessage)
    assert outbound.content == "Checking that now."
    assert session.events[-1].kind == "assistant"
    assert session.events[-1].tool_calls is not None
    assert session.events[-1].tool_calls[0]["function"]["name"] == "log"
    assert tracker.pending


def test_tool_call_tracker_interrupt_records_background_notice() -> None:
    session = Session(MessageAddress("telegram", "123"))
    tracker = ToolCallTracker()
    tracker.add("tc1", "web_search", None)  # type: ignore[arg-type]

    tracker.handle_interrupt(session)

    assert not tracker.pending
    assert session.events[-1].kind == "system"
    assert "still executing in the background" in str(session.events[-1].content)
