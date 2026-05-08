"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from benchclaw.agent.dump import dump_messages
from benchclaw.agent.loop_state import AddressState, BatchApplication, ToolCallTracker
from benchclaw.agent.prompt import PromptBuilder
from benchclaw.agent.response import ResponseHandler
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
    TypingEvent,
)
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, LLMResponse
from benchclaw.session import (
    Session,
    SessionManager,
    SystemEvent,
    UserEvent,
)

_COMPACT_THRESHOLD = 0.8


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
        self.prompt = PromptBuilder(
            config.workspace_path,
            tools=self.tools,
            media_repo=media_repo,
            agent_config=self.config,
        )
        self.response = ResponseHandler(bus, self.tools, self.config)

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
        logger.warning(
            f"Compacting session {addr}: {total_tokens}/{self.config.context_window} tokens"
        )
        session.compact(self.master_ctx.log_store)
        logger.warning(
            f"Session {addr} compacted: {len(session.events)} events remain, "
            f"compacted_through={session.compacted_through}"
        )

    async def _apply_llm_response(
        self,
        response: LLMResponse,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        self._maybe_compact_session(session, addr, response.usage.get("total_tokens", 0))
        await self.response.apply(response, session, tracker, call_ctx, addr)

    @staticmethod
    def _flush_pending_system_events(session: Session, state: AddressState) -> None:
        for content in state.pending_system_events:
            session.append(SystemEvent(content=content))
        state.pending_system_events.clear()

    def _apply_batch(
        self,
        batch: InboundMessageBatch,
        session: Session,
        tracker: ToolCallTracker,
        addr: MessageAddress,
        state: AddressState,
    ) -> BatchApplication:
        needs_llm = False
        start_typing = False

        for result in batch.tool_results:
            tracker.handle_result(result, session)
        if batch.tool_results and not tracker.pending:
            self._flush_pending_system_events(session, state)
            # Skip the follow-up LLM call after a lone terminal_when_lone
            # tool turn (e.g. send_media) — the tool already produced the
            # user-visible reply. Pending system events stay flushed so a
            # subsequent user message renders cleanly.
            if not tracker.take_terminal_when_lone():
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
            state.pending_media = list(user_event.media)
            state.iteration_count = 0
            needs_llm = True

        return BatchApplication(needs_llm=needs_llm, start_typing=start_typing)

    async def _process_llm_turn(
        self,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        pending_media: list[str] | None = None,
    ) -> None:
        if pending_media is None:
            pending_media = []
        build = self.prompt.build(session, addr, pending_media)
        llm_messages = build.messages
        dump_messages(self.debug_dump_path, llm_messages)
        if pending_media:
            pending_media.clear()
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
        state = AddressState()

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
                pending_media=state.pending_media,
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
