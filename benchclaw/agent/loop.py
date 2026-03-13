"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from benchclaw.agent.context import ContextBuilder
from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.mcp_manager import MCPManager
from benchclaw.agent.tools.memory import LogStore
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.bus import (
    InboundMessage,
    InboundMessageBatch,
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
    RenderOptions,
    Session,
    SessionManager,
    SystemEvent,
    ToolEvent,
    UserEvent,
)

_COMPACT_THRESHOLD = 0.8
_DEBUG_INLINE_IMAGE_URL_CHARS = 40


@dataclass
class _AddressState:
    iteration_count: int = 0
    pending_system_events: list[str] = field(default_factory=list)
    pending_images: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _BatchApplication:
    needs_llm: bool = False
    start_typing: bool = False


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
        session.append(
            ToolEvent(
                content=event.result,
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
            )
        )
        if event.tool_call_id in self._in_flight:
            del self._in_flight[event.tool_call_id]
            return not self._in_flight

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
        media_repo: MediaRepository,
        debug_dump_path: Path | None = None,
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
    def _collapse_user_messages(messages: list[InboundMessage]) -> UserEvent:
        if len(messages) == 1:
            message = messages[0]
            return UserEvent(
                timestamp=message.timestamp,
                content=message.content,
                sender_id=message.sender_id,
                media=message.media,
                media_metadata=message.media_metadata,
                metadata=message.metadata,
            )
        parts = [f"[{m.sender_id}] {m.content}" for m in messages if m.content]
        first = messages[0]
        return UserEvent(
            timestamp=first.timestamp,
            sender_id=first.sender_id,
            content="\n".join(parts),
            media=[path for m in messages for path in m.media],
            media_metadata=[item for m in messages for item in m.media_metadata],
            metadata=first.metadata,
        )

    def _build_system_prompt(
        self,
        addr: MessageAddress,
        session: Session,
    ) -> str:
        return self.context.build_system_prompt(
            self.tools.values(),
            addr.channel,
            addr.chat_id,
            session.describe_current_session(),
        )

    def _render_turn_messages(
        self,
        session: Session,
        addr: MessageAddress,
        pending_images: list[str],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        prompt = self._build_system_prompt(addr, session)
        provider_messages = session.render_llm_messages(
            prompt,
            self.media_repo,
            RenderOptions(pending_image_paths=list(pending_images) or None),
            max_messages=self.config.memory_window,
        )
        debug_messages = session.render_llm_messages(
            prompt,
            self.media_repo,
            RenderOptions(
                pending_image_paths=list(pending_images) or None,
                max_inline_image_url_chars=_DEBUG_INLINE_IMAGE_URL_CHARS,
            ),
            max_messages=self.config.memory_window,
        )
        return provider_messages, debug_messages

    async def _call_provider(
        self,
        addr: MessageAddress,
        llm_messages: list[dict[str, object]],
    ):
        try:
            return await self.provider.chat(
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
            return None

    def _maybe_compact_session(
        self, session: Session, addr: MessageAddress, total_tokens: int
    ) -> None:
        if total_tokens <= self.config.context_window * _COMPACT_THRESHOLD:
            return
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

    async def _apply_llm_response(
        self,
        response,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        self._maybe_compact_session(session, addr, response.usage.get("total_tokens", 0))
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

    @staticmethod
    def _flush_pending_system_events(session: Session, state: _AddressState) -> None:
        for content in state.pending_system_events:
            session.append(SystemEvent(content=content))
        state.pending_system_events.clear()

    def _apply_batch(
        self,
        batch: InboundMessageBatch,
        session: Session,
        tracker: ToolCallTracker,
        addr: MessageAddress,
        state: _AddressState,
    ) -> _BatchApplication:
        needs_llm = False
        start_typing = False

        for result in batch.tool_results:
            tracker.handle_result(result, session)
        if batch.tool_results and not tracker.pending:
            self._flush_pending_system_events(session, state)
            needs_llm = True

        for event in batch.system_events:
            if tracker.pending:
                logger.debug(f"SystemEvent buffered (tools in flight): {event.content[:60]}")
                state.pending_system_events.append(event.content)
            else:
                session.append(SystemEvent(content=event.content))
                needs_llm = True

        if batch.user_messages:
            start_typing = True
            if tracker.pending:
                tracker.handle_interrupt(session)
            self._flush_pending_system_events(session, state)

            user_event = self._collapse_user_messages(batch.user_messages)
            preview = (
                user_event.content[:80] + "..."
                if len(user_event.content) > 80
                else user_event.content
            )
            logger.info(f"Processing message from {addr}: {preview}")
            session.append(user_event)
            state.pending_images = list(user_event.media)
            state.iteration_count = 0
            needs_llm = True

        return _BatchApplication(needs_llm=needs_llm, start_typing=start_typing)

    async def _process_llm_turn(
        self,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        pending_images: list[str] | None = None,
    ) -> None:
        if pending_images is None:
            pending_images = []
        llm_messages, debug_messages = self._render_turn_messages(session, addr, pending_images)
        self._dump_messages(debug_messages)
        if pending_images:
            pending_images.clear()
        response = await self._call_provider(addr, llm_messages)
        if response is None:
            return
        await self._apply_llm_response(response, session, tracker, call_ctx, addr)

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
        state = _AddressState()

        while True:
            if not tracker.pending:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=False))

            batch = await self.bus.consume_inbound_batch(address=addr)
            batch_result = self._apply_batch(batch, session, tracker, addr, state)
            if batch_result.start_typing:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=True))
            if not batch_result.needs_llm:
                continue

            if state.iteration_count >= self.config.max_tool_iterations:
                logger.warning(f"Max tool iterations reached for {addr}")
                continue
            state.iteration_count += 1

            await self._process_llm_turn(
                session,
                tracker,
                call_ctx,
                addr,
                pending_images=state.pending_images,
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
