"""LLM response handling: tool dispatch, tool-result stringification, outbound publish.

The agent loop hands every :class:`LLMResponse` to :meth:`ResponseHandler.apply`,
which classifies it (tool calls / empty / text), updates the session, dispatches
background tools, and publishes the user-visible message.
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from benchclaw.agent.loop_state import ToolCallTracker
from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.bus import (
    MessageAddress,
    MessageBus,
    OutboundMessage,
    SystemMessageEvent,
    ToolResultEvent,
)
from benchclaw.config import AgentConfig
from benchclaw.providers.base import LLMResponse, ToolCallRequest
from benchclaw.session import AssistantEvent, Session


def stringify_tool_result(result: object) -> str:
    """Coerce a tool result to a string for the trace.

    We deliberately do NOT truncate or reflow here — display-time truncation
    belongs in the channel.
    """
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class ResponseHandler:
    """Applies one LLM response to the session and the bus."""

    def __init__(
        self,
        bus: MessageBus,
        tools: ToolRegistry,
        agent_config: AgentConfig,
    ) -> None:
        self.bus = bus
        self.tools = tools
        self.config = agent_config

    async def apply(
        self,
        response: LLMResponse,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        usage = response.usage
        logger.info(
            f"LLM response for {addr}: "
            f"{usage.get('prompt_tokens', '?')} prompt, "
            f"{usage.get('completion_tokens', '?')} completion, "
            f"{usage.get('total_tokens', '?')} total / {self.config.context_window} budget"
        )
        content = (response.content or "").rstrip("\n")

        if response.has_tool_calls:
            await self._dispatch_tool_calls(response, content, session, tracker, call_ctx, addr)
            return

        if not content:
            await self._handle_empty(addr)
            return

        session.append(AssistantEvent(content=content))
        preview = content[:120] + "..." if len(content) > 120 else content
        logger.info(f"Response to {addr}: {preview}")
        await self.bus.publish_outbound(OutboundMessage(address=addr, content=content))

    async def _dispatch_tool_calls(
        self,
        response: LLMResponse,
        content: str,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        tool_call_dicts = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in response.tool_calls
        ]
        session.append(
            AssistantEvent(
                content=content,
                tool_calls=tool_call_dicts,
                reasoning_content=response.reasoning_content,
            )
        )
        for tc in response.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info(f"Tool call (background): {tc.name}({args_str[:200]})")
            task = asyncio.create_task(
                self._run_tool_and_post(tc, call_ctx, addr),
                name=f"tool-{tc.id[:8]}",
            )
            tracker.add(tc.id, tc.name, task)
        if (
            len(response.tool_calls) == 1
            and (lone_tool := self.tools.get(response.tool_calls[0].name)) is not None
            and lone_tool.terminal_when_lone
        ):
            tracker.mark_turn_terminal_when_lone()
        if content:
            await self.bus.publish_outbound(OutboundMessage(address=addr, content=content))

    async def _run_tool_and_post(
        self,
        tc: ToolCallRequest,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        try:
            result = await self.tools.execute(tc.name, tc.arguments, call_ctx)
        except asyncio.CancelledError:
            result = "Cancelled."
        except Exception as e:
            result = f"Error executing {tc.name}: {e}"
        await self.bus.publish_inbound(
            addr,
            ToolResultEvent(tool_call_id=tc.id, tool_name=tc.name, result=result),
        )

    async def _handle_empty(self, addr: MessageAddress) -> None:
        logger.warning(
            f"LLM returned empty response (no text, no tool calls) for {addr} — injecting nudge"
        )
        await self.bus.publish_inbound(
            addr,
            SystemMessageEvent(
                content="You did not provide a text response. Please respond to the user now."
            ),
        )
