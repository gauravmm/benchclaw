"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Literal

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
    ToolResultEvent,
    TypingEvent,
)
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, ToolCallRequest
from benchclaw.session import (
    AssistantEvent,
    ConversationEvent,
    Session,
    SessionManager,
    SystemEvent,
    ToolEvent,
    UserEvent,
)

_COMPACT_THRESHOLD = 0.8


class ToolCallTracker:
    """Per-address tracker for in-flight background tool calls."""

    def __init__(self) -> None:
        self._in_flight: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def tasks(self) -> dict[str, asyncio.Task]:
        return self._tasks

    @property
    def pending(self) -> bool:
        return bool(self._in_flight)

    def add(self, tool_call_id: str, tool_name: str, task: asyncio.Task) -> None:
        self._in_flight[tool_call_id] = tool_name
        self._tasks[tool_call_id] = task

    def handle_interrupt(self, session: Session) -> None:
        if not self._in_flight:
            return
        tool_list = ", ".join(f"{name} ({tid[:8]})" for tid, name in self._in_flight.items())
        session.append(
            SystemEvent(
                content="The following tools are still executing in the background: "
                f"{tool_list}. Their results will arrive as new events."
            )
        )
        self._in_flight.clear()

    def handle_result(self, event: ToolResultEvent, session: Session) -> bool:
        self._tasks.pop(event.tool_call_id, None)
        if event.tool_call_id in self._in_flight:
            del self._in_flight[event.tool_call_id]
            session.append(
                ToolEvent(
                    content=event.result,
                    tool_call_id=event.tool_call_id,
                    tool_name=event.tool_name,
                )
            )
            return not self._in_flight

        session.append(
            ToolEvent(
                content=event.result,
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
            )
        )
        session.append(
            SystemEvent(
                content=f"Background tool '{event.tool_name}' completed. Review the result and update the user if useful."
            )
        )
        return True


class AgentLoop:
    """Event-driven agent runtime."""

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        provider: LLMProvider,
        debug_dump_path: Path | None = None,
        media_repo: MediaRepository | None = None,
    ):
        self.workspace_path = config.workspace_path
        self.config = config.agents.master
        self.bus = bus
        self.provider = provider
        self.debug_dump_path = debug_dump_path
        self.media_repo = media_repo

        self.context = ContextBuilder(config.workspace_path)
        self.sessions = SessionManager(config.workspace_path / "sessions")

        master_ctx = ToolContext(
            workspace=config.workspace_path,
            bus=bus,
            log_store=LogStore(config.workspace_path),
            media_repo=media_repo,
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

    def _dump_messages(self, messages: list[dict[str, object]]) -> None:
        if not self.debug_dump_path:
            return

        try:
            self.debug_dump_path.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to write debug dump: {e}")

    @staticmethod
    def _merge_user_messages(messages: list[InboundMessage]) -> InboundMessage:
        if len(messages) == 1:
            return messages[0]
        parts = [f"[{m.sender_id}] {m.content}" for m in messages if m.content]
        first = messages[0]
        return InboundMessage(
            address=first.address,
            sender_id=first.sender_id,
            content="\n".join(parts),
            timestamp=first.timestamp,
            media=[path for m in messages for path in m.media],
            media_metadata=[item for m in messages for item in m.media_metadata],
            metadata=first.metadata,
        )

    @staticmethod
    def _render_message_content(
        content: object, profile: Literal["raw", "provider", "debug"]
    ) -> object:
        if profile != "debug":
            return content
        if isinstance(content, list):
            return [AgentLoop._render_message_content(item, profile) for item in content]
        if isinstance(content, dict):
            if content.get("type") == "image_url":
                url = (content.get("image_url") or {}).get("url", "")
                truncated = url[:40] + "…" if len(url) > 40 else url
                return {"type": "image_url", "image_url": {"url": truncated}}
            return {
                key: AgentLoop._render_message_content(value, profile)
                for key, value in content.items()
            }
        return content

    def _render_event_message(
        self,
        event: ConversationEvent,
        *,
        profile: Literal["raw", "provider", "debug"],
        include_reasoning: bool,
        pending_image_blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        if isinstance(event, AssistantEvent):
            message = event.to_llm_message(include_reasoning=include_reasoning)
        elif isinstance(event, UserEvent):
            message = event.to_llm_message(pending_image_blocks=pending_image_blocks or [])
        else:
            message = event.to_llm_message()
        message["content"] = self._render_message_content(message.get("content", ""), profile)
        return message

    def _build_llm_messages(
        self,
        session: Session,
        addr: MessageAddress,
        *,
        pending_images: list[str] | None = None,
        profile: Literal["provider", "debug"] = "provider",
    ) -> list[dict[str, object]]:
        prompt = self.context.build_system_prompt(
            self.tools.values(),
            addr.channel,
            addr.chat_id,
            session.describe_current_session(),
        )
        history = session.get_history_events(self.config.memory_window)
        last_reasoning_idx = next(
            (
                i
                for i, event in reversed(list(enumerate(history)))
                if isinstance(event, AssistantEvent) and event.reasoning_content
            ),
            None,
        )
        pending_image_blocks = (
            (self.media_repo or MediaRepository(self.workspace_path)).build_image_blocks(
                pending_images
            )
            if pending_images
            else None
        )
        messages: list[dict[str, object]] = [{"role": "system", "content": prompt}]
        for i, event in enumerate(history):
            messages.append(
                self._render_event_message(
                    event,
                    profile=profile,
                    include_reasoning=i == last_reasoning_idx,
                    pending_image_blocks=(pending_image_blocks if i == len(history) - 1 else None),
                )
            )
        return messages

    async def _process_llm_turn(
        self,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        pending_images: list[str] | None = None,
    ) -> None:
        llm_messages = self._build_llm_messages(
            session,
            addr,
            pending_images=pending_images,
            profile="provider",
        )
        self._dump_messages(
            self._build_llm_messages(session, addr, pending_images=pending_images, profile="debug")
        )
        if pending_images:
            pending_images.clear()
        started_at = time.monotonic()
        try:
            response = await self.provider.chat(
                messages=llm_messages,
                tools=self.tools.get_definitions(),
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        except Exception as e:
            logger.error(f"LLM error for {addr}: {e}")
            await self.bus.publish_outbound(
                OutboundMessage(address=addr, content=f"Sorry, I encountered an error: {e}")
            )
            return
        _elapsed = time.monotonic() - started_at

        total_tokens = response.usage.get("total_tokens", 0)
        if total_tokens > self.config.context_window * _COMPACT_THRESHOLD:
            logger.info(
                "Session compaction triggered for %s: token usage %s/%s",
                addr,
                total_tokens,
                self.config.context_window,
            )
            session.compact(self.master_ctx.log_store)
            logger.info(
                "Context compacted (%s events, compacted_through=%s)",
                len(session.events),
                session.compacted_through,
            )

        visible_content = response.content or ""
        if response.has_tool_calls:
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
                    content=visible_content,
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
            if visible_content:
                await self.bus.publish_outbound(
                    OutboundMessage(address=addr, content=visible_content)
                )
            return

        final = visible_content or "I've completed processing but have no response to give."
        session.append(AssistantEvent(content=final))
        preview = final[:120] + "..." if len(final) > 120 else final
        logger.info(f"Response to {addr}: {preview}")
        await self.bus.publish_outbound(OutboundMessage(address=addr, content=final))

    async def _address_loop(self, addr: MessageAddress) -> None:
        session = self.sessions.get(addr)
        tracker = ToolCallTracker()
        call_ctx = ToolContext(
            workspace=self.tools._master_ctx.workspace,
            bus=self.bus,
            log_store=self.tools._master_ctx.log_store,
            media_repo=self.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        iteration_count = 0
        pending_system_events: list[str] = []
        pending_images: list[str] = []

        while True:
            if not tracker.pending:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=False))

            batch = await self.bus.consume_inbound_batch(address=addr)
            needs_llm = False

            for result in batch.tool_results:
                tracker.handle_result(result, session)
            if batch.tool_results and not tracker.pending:
                for content in pending_system_events:
                    session.append(SystemEvent(content=content))
                pending_system_events.clear()
                needs_llm = True

            for event in batch.system_events:
                if tracker.pending:
                    logger.debug(f"SystemEvent buffered (tools in flight): {event.content[:60]}")
                    pending_system_events.append(event.content)
                else:
                    session.append(SystemEvent(content=event.content))
                    needs_llm = True

            if batch.user_messages:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=True))
                if tracker.pending:
                    tracker.handle_interrupt(session)
                for content in pending_system_events:
                    session.append(SystemEvent(content=content))
                pending_system_events.clear()

                msg = self._merge_user_messages(batch.user_messages)
                preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
                logger.info(f"Processing message from {addr}: {preview}")
                session.append(
                    UserEvent(
                        timestamp=msg.timestamp,
                        content=msg.content,
                        sender_id=msg.sender_id,
                        media=msg.media,
                        media_metadata=msg.media_metadata,
                        metadata=msg.metadata,
                    )
                )
                pending_images = list(msg.media)
                iteration_count = 0
                needs_llm = True

            if not needs_llm:
                continue

            if iteration_count >= self.config.max_tool_iterations:
                logger.warning(f"Max tool iterations reached for {addr}")
                continue
            iteration_count += 1

            await self._process_llm_turn(
                session,
                tracker,
                call_ctx,
                addr,
                pending_images=pending_images,
            )

    async def run(self) -> None:
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
                    await asyncio.get_event_loop().create_future()
                except asyncio.CancelledError:
                    for task in [dispatch_task, *addr_tasks.values()]:
                        task.cancel()
                    await asyncio.gather(
                        dispatch_task, *addr_tasks.values(), return_exceptions=True
                    )
