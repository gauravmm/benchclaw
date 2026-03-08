"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from benchclaw.agent.context import ContextBuilder
from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.mcp_manager import MCPManager
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.bus import (
    InboundMessage,
    MessageAddress,
    MessageBus,
    OutboundMessage,
    ToolResultEvent,
)
from benchclaw.config import Config
from benchclaw.providers.base import LLMProvider, ToolCallRequest
from benchclaw.session import SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Subscribes to per-address inbound queues from the bus
    2. For each address, runs an event-driven loop that processes both user
       messages (InboundMessage) and background tool results (ToolResultEvent)
    3. Calls the LLM whenever there is something to process
    4. Dispatches tool calls as background asyncio tasks; results are posted
       back via bus.publish_tool_result() when done
    5. Sends final responses back via the bus
    """

    def __init__(self, config: Config, bus: MessageBus, provider: LLMProvider):
        self.workspace_path = config.workspace_path
        self.config = config.agents.master
        self.bus = bus
        self.provider = provider

        self.context = ContextBuilder(config.workspace_path)
        self.sessions = SessionManager(config.workspace_path / "sessions")

        # self.subagents = SubagentManager(config=config, provider=provider, bus=bus)

        master_ctx = ToolContext(
            workspace=config.workspace_path,
            bus=bus,
            # subagent_manager=self.subagents,
        )
        mcp_manager = MCPManager(config.mcp_servers) if config.mcp_servers else None
        self.tools = ToolRegistry(config.tools, master_ctx, mcp_manager=mcp_manager)

    async def _run_tool_and_post(
        self,
        tc: ToolCallRequest,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        """Execute a single tool call and post a ToolResultEvent to the bus when done."""
        try:
            result = await self.tools.execute(tc.name, tc.arguments, call_ctx)
        except asyncio.CancelledError:
            result = "Cancelled."
        except Exception as e:
            result = f"Error executing {tc.name}: {e}"
        await self.bus.publish_tool_result(
            addr,
            ToolResultEvent(tool_call_id=tc.id, tool_name=tc.name, result=result),
        )

    async def _address_loop(self, addr: MessageAddress) -> None:
        """
        Event-driven processing loop for a single address.

        Reads AddressEvent (InboundMessage | ToolResultEvent) directly from
        bus.inbound[addr], calling the LLM after each event and dispatching
        tool calls as background tasks via _run_tool_and_post.

        in_flight tracks tool call IDs currently executing. When a user message
        arrives while tools are running, synthetic results are added to satisfy
        the API format, and a system message lists which tools are still running.
        """
        session = self.sessions.get(addr)
        call_ctx = ToolContext(
            workspace=self.tools._master_ctx.workspace,
            bus=self.bus,
            address=addr,
        )
        in_flight: dict[str, str] = {}  # tool_call_id -> tool_name
        iteration_count = 0

        while True:
            event = await self.bus.consume_inbound(address=addr)

            if isinstance(event, InboundMessage):
                if in_flight:
                    # Satisfy the API format (tool results required before next user message),
                    # then tell the LLM which tools are still running in the background.
                    for tool_id, tool_name in in_flight.items():
                        session.live_messages.append(
                            self.context.tool_result(
                                tool_id, tool_name, "[executing in background]"
                            )
                        )
                    tool_list = ", ".join(f"{name} ({tid[:8]})" for tid, name in in_flight.items())
                    session.live_messages.append(
                        {
                            "role": "system",
                            "content": f"The following tools are still executing in the background: {tool_list}. Their results will arrive as new messages.",
                        }
                    )
                    in_flight.clear()

                preview = event.content[:80] + "..." if len(event.content) > 80 else event.content
                logger.info(f"Processing message from {addr}: {preview}")

                if not session.live_messages:
                    session.live_messages = self.context.build_messages(
                        history=session.get_history(max_messages=self.config.memory_window),
                        current_message=event.content,
                        tools=self.tools,
                        media=event.media or None,
                        channel=event.channel,
                        chat_id=event.chat_id,
                    )
                else:
                    session.live_messages.append({"role": "user", "content": event.content})

                session.add_message("user", event.content)
                iteration_count = 0

            elif isinstance(event, ToolResultEvent):
                if event.tool_call_id in in_flight:
                    del in_flight[event.tool_call_id]
                    session.live_messages.append(
                        self.context.tool_result(event.tool_call_id, event.tool_name, event.result)
                    )
                    if in_flight:
                        continue  # still waiting for other tools in this batch
                else:
                    # Iteration was closed by a user message; deliver as background notification.
                    notification = (
                        f"[Background tool '{event.tool_name}' completed]:\n{event.result}"
                    )
                    session.live_messages.append({"role": "user", "content": notification})
                    session.add_message("user", notification)

            # Check if the iterations have maxed out.
            if iteration_count >= self.config.max_tool_iterations:
                logger.warning(f"Max tool iterations reached for {addr}")
                continue
            iteration_count += 1

            # Call LLM (shared path for both event types)
            try:
                response = await self.provider.chat(
                    messages=session.live_messages,
                    tools=self.tools.get_definitions(master=True),
                    model=self.config.model,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
            except Exception as e:
                logger.error(f"LLM error for {addr}: {e}")
                await self.bus.publish_outbound(
                    OutboundMessage(
                        address=addr,
                        content=f"Sorry, I encountered an error: {e}",
                    )
                )
                continue

            # Process LLM response
            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in response.tool_calls
                ]
                session.live_messages.append(
                    self.context.assistant_message(
                        response.content,
                        tool_call_dicts,
                        reasoning_content=response.reasoning_content,
                    )
                )

                for tc in response.tool_calls:
                    in_flight[tc.id] = tc.name
                    args_str = json.dumps(tc.arguments, ensure_ascii=False)
                    logger.info(f"Tool call (background): {tc.name}({args_str[:200]})")
                    asyncio.create_task(
                        self._run_tool_and_post(tc, call_ctx, addr),
                        name=f"tool-{tc.id[:8]}",
                    )

                if response.content:
                    await self.bus.publish_outbound(
                        OutboundMessage(address=addr, content=response.content)
                    )
            else:
                final = (
                    response.content or "I've completed processing but have no response to give."
                )
                session.live_messages.append(self.context.assistant_message(final))
                session.add_message("assistant", final)
                preview = final[:120] + "..." if len(final) > 120 else final
                logger.info(f"Response to {addr}: {preview}")
                await self.bus.publish_outbound(OutboundMessage(address=addr, content=final))

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        async with self.sessions:
            async with self.tools:
                logger.info("Agent loop started")
                new_addr_queue = self.bus.subscribe_new_addresses()
                addr_tasks: dict[MessageAddress, asyncio.Task] = {}

                async def _dispatch() -> None:
                    while True:
                        addr = await new_addr_queue.get()
                        addr_tasks[addr] = asyncio.create_task(
                            self._address_loop(addr), name=f"agent-{addr}"
                        )

                dispatch_task = asyncio.create_task(_dispatch())
                try:
                    await asyncio.get_event_loop().create_future()  # run forever
                except asyncio.CancelledError:
                    for t in [dispatch_task, *addr_tasks.values()]:
                        t.cancel()
                    await asyncio.gather(
                        dispatch_task,
                        *addr_tasks.values(),
                        return_exceptions=True,
                    )
