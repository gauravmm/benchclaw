"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from benchclaw.agent.context import ContextBuilder
from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.mcp_manager import MCPManager
from benchclaw.agent.tools.memory import LogStore
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.bus import (
    InboundMessage,
    MessageAddress,
    MessageBus,
    OutboundMessage,
    SystemEvent,
    ToolResultEvent,
)
from benchclaw.config import Config
from benchclaw.providers.base import LLMProvider, ToolCallRequest
from benchclaw.session import Session, SessionManager

# Compact the context when token usage exceeds this fraction of the context window.
_COMPACT_THRESHOLD = 0.8


class ToolCallTracker:
    """
    Per-address tracker for in-flight background tool calls.

    Encapsulates the in_flight dict and asyncio.Task handles so that
    _address_loop can delegate all tool-call bookkeeping to method calls.
    """

    def __init__(self, context: ContextBuilder) -> None:
        self._context = context
        self._in_flight: dict[str, str] = {}  # tool_call_id -> tool_name
        self._tasks: dict[str, asyncio.Task] = {}  # tool_call_id -> Task

    @property
    def tasks(self) -> dict[str, asyncio.Task]:
        """Live dict of task handles — assigned to ToolContext.background_tasks."""
        return self._tasks

    @property
    def pending(self) -> bool:
        return bool(self._in_flight)

    def add(self, tool_call_id: str, tool_name: str, task: asyncio.Task) -> None:
        self._in_flight[tool_call_id] = tool_name
        self._tasks[tool_call_id] = task

    def handle_interrupt(self, session: Session) -> None:
        """
        User message arrived while tools are still running.

        Appends synthetic tool results and a system message, then clears
        in_flight. Tasks keep running; their results become background notifications.
        """
        for tool_id, tool_name in self._in_flight.items():
            session.live_messages.append(
                self._context.tool_result(tool_id, tool_name, "[executing in background]")
            )
        tool_list = ", ".join(f"{name} ({tid[:8]})" for tid, name in self._in_flight.items())
        session.live_messages.append(
            {
                "role": "system",
                "content": (
                    f"The following tools are still executing in the background: "
                    f"{tool_list}. Their results will arrive as new messages."
                ),
            }
        )
        self._in_flight.clear()
        # _tasks intentionally kept — tasks still running; kill tool may need handles.

    def handle_result(self, event: ToolResultEvent, session: Session) -> bool:
        """
        Handle a ToolResultEvent.

        Returns True  → caller should proceed to LLM call.
        Returns False → caller should `continue` (still waiting for sibling tools).
        """
        if event.tool_call_id in self._in_flight:
            del self._in_flight[event.tool_call_id]
            self._tasks.pop(event.tool_call_id, None)
            session.live_messages.append(
                self._context.tool_result(event.tool_call_id, event.tool_name, event.result)
            )
            return not self._in_flight  # True when last sibling returned
        else:
            # Interrupt happened earlier; deliver result as background notification.
            notification = f"[Background tool '{event.tool_name}' completed]:\n{event.result}"
            session.add_message("user", notification)
            return True


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

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        provider: LLMProvider,
        debug_dump_path: Path | None = None,
    ):
        self.workspace_path = config.workspace_path
        self.config = config.agents.master
        self.bus = bus
        self.provider = provider
        self.debug_dump_path = debug_dump_path

        self.context = ContextBuilder(config.workspace_path)
        self.sessions = SessionManager(config.workspace_path / "sessions")

        # self.subagents = SubagentManager(config=config, provider=provider, bus=bus)

        master_ctx = ToolContext(
            workspace=config.workspace_path,
            bus=bus,
            log_store=LogStore(config.workspace_path),
            # subagent_manager=self.subagents,
        )
        self.master_ctx = master_ctx
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

    def _dump_messages(self, messages: list[dict]) -> None:
        """Write the LLM input messages to the debug dump file."""
        if self.debug_dump_path:
            try:
                self.debug_dump_path.write_text(
                    json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as e:
                logger.warning(f"Failed to write debug dump: {e}")

    def _compact_context(
        self,
        session: Session,
        addr: MessageAddress,
        channel: str | None,
        chat_id: str | None,
    ) -> None:
        """
        Compact live_messages when the context window is filling up.

        Reads recent log entries and rebuilds the context as:
          - System prompt
          - A summary injected as a system message with recent log entries
          - The last memory_window messages from the persistent session history
        """
        log_store = self.master_ctx.log_store
        summary_content = (
            "[Context compacted to stay within context window limits.]\nRecent activity log:\n"
            + (log_store.read_recent(n=20) if log_store else "[No logs available]")
        )
        new_live: list[dict] = [
            {
                "role": "system",
                "content": self.context.build_system_prompt(self.tools.values(), channel, chat_id),
            },
            {"role": "system", "content": summary_content},
            *session.get_history(max_messages=self.config.memory_window),
        ]
        session.live_messages = new_live
        session.last_consolidated = len(session.messages)
        logger.info(f"Context compacted for {addr} ({len(new_live)} messages after compaction)")

    async def _address_loop(self, addr: MessageAddress) -> None:
        """
        Event-driven processing loop for a single address.

        Reads AddressEvent (InboundMessage | ToolResultEvent) directly from
        bus.inbound[addr], calling the LLM after each event and dispatching
        tool calls as background tasks via _run_tool_and_post.

        ToolCallTracker manages tool call IDs and asyncio.Task handles. When a
        user message arrives while tools are running, the tracker injects synthetic
        results and a system note, then clears in_flight (tasks keep running).
        """
        session = self.sessions.get(addr)
        tracker = ToolCallTracker(self.context)
        call_ctx = ToolContext(
            workspace=self.tools._master_ctx.workspace,
            bus=self.bus,
            log_store=self.tools._master_ctx.log_store,
            address=addr,
            background_tasks=tracker.tasks,
        )
        iteration_count = 0
        channel = addr.channel
        chat_id = addr.chat_id
        session.live_messages = self.context.build_context(
            history=session.get_history(max_messages=self.config.memory_window),
            tools=self.tools,
            channel=channel,
            chat_id=chat_id,
        )

        while True:
            event = await self.bus.consume_inbound(address=addr)

            if isinstance(event, InboundMessage):
                if tracker.pending:
                    tracker.handle_interrupt(session)

                preview = event.content[:80] + "..." if len(event.content) > 80 else event.content
                logger.info(f"Processing message from {addr}: {preview}")
                session.live_messages.append(
                    {
                        "role": "system",
                        "content": datetime.now()
                        .astimezone()
                        .strftime("Current time: %Y-%m-%d %H:%M (%A) %z"),
                    }
                )
                session.add_message(
                    "user",
                    event.content,
                    sender_id=event.sender_id,
                    media=event.media,
                    media_metadata=event.media_metadata,
                    metadata=event.metadata,
                )
                iteration_count = 0

            elif isinstance(event, SystemEvent):
                session.live_messages.append({"role": "system", "content": event.content})

            elif isinstance(event, ToolResultEvent):
                if not tracker.handle_result(event, session):
                    continue  # still waiting for other tools in this batch

            # Check if the iterations have maxed out.
            if iteration_count >= self.config.max_tool_iterations:
                logger.warning(f"Max tool iterations reached for {addr}")
                continue
            iteration_count += 1

            # Dump input messages to the debug file if requested.
            self._dump_messages(session.live_messages)

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

            # Check token usage and compact if approaching context window limit.
            total_tokens = response.usage.get("total_tokens", 0)
            if total_tokens > self.config.context_window * _COMPACT_THRESHOLD:
                logger.info(
                    "Session compaction triggered for "
                    f"{addr}: token usage {total_tokens}/{self.config.context_window}"
                )
                self._compact_context(session, addr, channel, chat_id)

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
                    args_str = json.dumps(tc.arguments, ensure_ascii=False)
                    logger.info(f"Tool call (background): {tc.name}({args_str[:200]})")
                    task = asyncio.create_task(
                        self._run_tool_and_post(tc, call_ctx, addr),
                        name=f"tool-{tc.id[:8]}",
                    )
                    tracker.add(tc.id, tc.name, task)

                if response.content:
                    await self.bus.publish_outbound(
                        OutboundMessage(address=addr, content=response.content)
                    )
            else:
                final = (
                    response.content or "I've completed processing but have no response to give."
                )
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
