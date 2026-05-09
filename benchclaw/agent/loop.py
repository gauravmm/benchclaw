"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from loguru import logger

from benchclaw.agent.cache_monitor import PromptCacheMonitor
from benchclaw.agent.compactor import Compactor
from benchclaw.agent.dump import dump_messages
from benchclaw.agent.loop_state import AddressState, ToolCallTracker
from benchclaw.agent.prompt import PromptBuilder
from benchclaw.agent.response import ResponseHandler
from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.cron.tool import CronTool
from benchclaw.agent.tools.mcp_manager import MCPManager
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
    AssistantEvent,
    Session,
    SessionManager,
    SystemEvent,
    UserEvent,
)


class AgentLoop:
    """Event-driven agent runtime."""

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        provider: LLMProvider,
        media_repo: MediaRepository,
        debug_dump_dir: Path | None = None,
    ):
        self.workspace_path = config.workspace_path
        self.config = config.agents.master
        self.bus = bus
        self.provider = provider
        self.debug_dump_dir = debug_dump_dir
        self.media_repo = media_repo

        self.sessions = SessionManager(config.workspace_path / "sessions")

        master_ctx = ToolContext(
            workspace=config.workspace_path,
            bus=bus,
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
        self.compactor = Compactor(self.config)
        self.cache_monitor = PromptCacheMonitor(log_dir=debug_dump_dir)

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
                top_p=self.config.top_p,
                top_k=self.config.top_k,
                enable_thinking=self.config.enable_thinking,
            )
        except Exception as e:
            logger.error(f"LLM error for {addr}: {e}")
            await self.bus.publish_outbound(
                OutboundMessage(address=addr, content=f"Sorry, I encountered an error: {e}")
            )
            return None

    async def _apply_llm_response(
        self,
        response: LLMResponse,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
    ) -> None:
        self.compactor.maybe_compact(session, addr, response.usage.get("total_tokens", 0))
        await self.response.apply(response, session, tracker, call_ctx, addr)

    @staticmethod
    def _flush_pending_system_events(session: Session, state: AddressState) -> None:
        for content in state.pending_system_events:
            session.append(SystemEvent(content=content))
        state.pending_system_events.clear()

    async def _apply_batch(
        self,
        batch: InboundMessageBatch,
        session: Session,
        tracker: ToolCallTracker,
        addr: MessageAddress,
        state: AddressState,
    ) -> bool:
        """Apply one inbound batch to the session and return whether the
        address loop should run an LLM turn after this batch."""
        needs_llm = False

        for result in batch.tool_results:
            tracker.handle_result(result, session)
        if batch.tool_results and not tracker.pending:
            self._flush_pending_system_events(session, state)
            # Skip the follow-up LLM call after a lone terminal_when_lone
            # tool turn (e.g. send_media) — the tool already produced the
            # user-visible reply.
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
            await self.bus.publish_outbound(TypingEvent(addr, is_typing=True))
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

        return needs_llm

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
        self.cache_monitor.observe(addr, build)
        llm_messages = build.messages
        if pending_media:
            pending_media.clear()
        response = await self._call_provider(addr, llm_messages)
        if response is None:
            return
        await self._apply_llm_response(response, session, tracker, call_ctx, addr)
        if isinstance(session.events[-1], AssistantEvent):
            llm_messages = [*llm_messages, session.events[-1].to_llm_message()]
        dump_messages(self.debug_dump_dir, addr, llm_messages)

    async def _address_loop(self, addr: MessageAddress) -> None:
        session = self.sessions.get(addr)
        tracker = ToolCallTracker()
        call_ctx = ToolContext(
            workspace=self.tools._master_ctx.workspace,
            bus=self.bus,
            media_repo=self.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        state = AddressState()

        while True:
            if not tracker.pending:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=False))

            batch = await self.bus.consume_inbound_batch(address=addr)
            needs_llm = await self._apply_batch(batch, session, tracker, addr, state)
            if not needs_llm:
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
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(self.sessions)
            await stack.enter_async_context(self.tools)
            logger.info("Agent loop started")
            new_addr_queue = self.bus.subscribe_new_addresses()

            try:
                async with asyncio.TaskGroup() as tg:

                    async def _dispatch() -> None:
                        while True:
                            addr = await new_addr_queue.get()
                            tg.create_task(self._address_loop(addr), name=f"agent-{addr}")

                    tg.create_task(_dispatch(), name="agent-dispatch")
                    cron_tool = self.tools.get("cron")
                    if isinstance(cron_tool, CronTool):
                        tg.create_task(cron_tool.run_loop(), name="cron-loop")
            except* asyncio.CancelledError:
                # Cooperative shutdown: TaskGroup has already cancelled and
                # awaited every per-address task plus the dispatcher; swallow
                # the re-raised group so callers see a clean exit.
                pass
