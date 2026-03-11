"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
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
    ToolResultEvent,
    TypingEvent,
)
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, ToolCallRequest
from benchclaw.session import Session, SessionManager

# Compact the context when token usage exceeds this fraction of the context window.
_COMPACT_THRESHOLD = 0.8

# Regex for extracting model-authored plan tags from response content.
_PLAN_TAG_RE = re.compile(r"<plan>(.*?)</plan>", re.DOTALL | re.IGNORECASE)

# Regex for extracting image caption tags emitted by the model.
_IMAGE_CAPTION_RE = re.compile(
    r'<image_caption\s+path="([^"]+)">(.*?)</image_caption>', re.DOTALL | re.IGNORECASE
)


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
       back via bus.publish_inbound() when done
    5. Sends final responses back via the bus
    """

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
        await self.bus.publish_inbound(
            addr,
            ToolResultEvent(tool_call_id=tc.id, tool_name=tc.name, result=result),
        )

    def _dump_messages(self, messages: list[dict]) -> None:
        """Write the LLM input messages to the debug dump file, stripping image data."""
        if not self.debug_dump_path:
            return

        def _strip_images(obj: object) -> object:
            if isinstance(obj, list):
                return [_strip_images(item) for item in obj]
            if isinstance(obj, dict):
                if obj.get("type") == "image_url":
                    url = (obj.get("image_url") or {}).get("url", "")
                    truncated = url[:40] + "…" if len(url) > 40 else url
                    return {"type": "image_url", "image_url": {"url": truncated}}
                return {k: _strip_images(v) for k, v in obj.items()}
            return obj

        try:
            self.debug_dump_path.write_text(
                json.dumps(_strip_images(messages), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to write debug dump: {e}")

    @staticmethod
    def _extract_plan(content: str) -> tuple[str, str | None]:
        """Extract <plan>...</plan> tags from response content.

        Returns (content_without_tags, plan_text_or_none). The plan text is
        not shown to the user; it is injected as a system message at the top
        of the next LLM call so the model can guide its own future steps.
        """
        matches = _PLAN_TAG_RE.findall(content)
        if not matches:
            return content, None
        plan = "\n".join(m.strip() for m in matches)
        cleaned = _PLAN_TAG_RE.sub("", content).strip()
        return cleaned, plan

    @staticmethod
    def _build_image_blocks(paths: list[str], workspace: Path) -> list[dict]:
        """Base64-encode image files and return image_url content blocks for the LLM."""
        blocks = []
        for path_str in paths:
            p = workspace / path_str
            mime, _ = mimetypes.guess_type(path_str)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                logger.warning(f"Skipping non-image or missing file: {path_str}")
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        return blocks

    @staticmethod
    def _merge_user_messages(messages: list[InboundMessage]) -> InboundMessage:
        """Merge a batch of user messages into one.

        A single message is returned as-is. Multiple messages are combined with
        per-sender attribution, using the first message's address and metadata.
        """
        if len(messages) == 1:
            return messages[0]
        parts = [f"[{m.sender_id}] {m.content}" for m in messages if m.content]
        media = [path for m in messages for path in m.media]
        media_metadata = [mm for m in messages for mm in m.media_metadata]
        first = messages[0]
        return InboundMessage(
            address=first.address,
            sender_id=first.sender_id,
            content="\n".join(parts),
            timestamp=first.timestamp,
            media=media,
            media_metadata=media_metadata,
            metadata=first.metadata,
        )

    @staticmethod
    def _extract_image_captions(content: str) -> tuple[str, dict[str, str]]:
        """Extract <image_caption path="...">...</image_caption> tags from response content.

        Returns (cleaned_content, {path: caption}). The path is workspace-relative
        (e.g. "media/a3f7b2c1/0310/1423-01.jpg") as emitted by the model.
        """
        captions: dict[str, str] = {}

        def _store(m: re.Match) -> str:
            captions[m.group(1)] = m.group(2).strip()
            return ""

        cleaned = _IMAGE_CAPTION_RE.sub(_store, content).strip()
        return cleaned, captions

    # Keep only this many chars of the most recent reasoning_content.
    # Thinking models require it in the immediately preceding turn, but the
    # full blob can be thousands of words and primes further circular reasoning.
    _MAX_REASONING_CHARS = 500

    @staticmethod
    def _strip_old_reasoning(messages: list[dict]) -> list[dict]:
        """
        Return messages with reasoning_content stripped or truncated.

        Thinking models (Qwen, etc.) require reasoning_content in the immediately
        preceding assistant turn, but replaying old or verbose blobs balloons the
        context and primes the model to continue prior circular reasoning.

        - All but the last assistant message with reasoning_content: stripped.
        - The last one: truncated to _MAX_REASONING_CHARS.
        """
        last_idx = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if messages[i].get("role") == "assistant" and messages[i].get("reasoning_content")
            ),
            None,
        )
        if last_idx is None:
            return messages
        result = []
        for i, m in enumerate(messages):
            if m.get("role") == "assistant" and "reasoning_content" in m:
                if i != last_idx:
                    m = {k: v for k, v in m.items() if k != "reasoning_content"}
                elif len(m["reasoning_content"]) > AgentLoop._MAX_REASONING_CHARS:
                    m = dict(m)
                    m["reasoning_content"] = (
                        m["reasoning_content"][: AgentLoop._MAX_REASONING_CHARS] + " [truncated]"
                    )
            result.append(m)
        return result

    def _compact_context(self, session: Session, addr: MessageAddress) -> None:
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
                "content": self.context.build_system_prompt(
                    self.tools.values(), addr.channel, addr.chat_id
                ),
            },
            {"role": "system", "content": summary_content},
            *session.get_history(max_messages=self.config.memory_window),
        ]
        session.live_messages = new_live
        session.last_consolidated = len(session.messages)
        logger.info(f"Context compacted for {addr} ({len(new_live)} messages after compaction)")

    async def _process_llm_turn(
        self,
        session: Session,
        tracker: ToolCallTracker,
        call_ctx: ToolContext,
        addr: MessageAddress,
        current_plan: str | None,
        pending_images: list[str] | None = None,
        media_repo: MediaRepository | None = None,
    ) -> str | None:
        """
        Prepare messages, call the LLM, handle compaction, and dispatch the response.

        Returns the new current_plan for the next turn (or None).
        On LLM error, publishes an error message and returns None.
        """
        self._dump_messages(session.live_messages)

        # Build the message list for this LLM call, injecting the plan if set.
        llm_messages = self._strip_old_reasoning(session.live_messages)
        if current_plan:
            llm_messages.append(
                {
                    "role": "system",
                    "content": f"Your plan from the previous turn:\n{current_plan}",
                }
            )

        # Inject pending images ephemerally into the last message (not stored in live_messages).
        if pending_images:
            blocks = self._build_image_blocks(pending_images, self.workspace_path)
            if blocks and llm_messages:
                last = llm_messages[-1]
                text = last["content"] if isinstance(last["content"], str) else ""
                llm_messages[-1] = {**last, "content": blocks + [{"type": "text", "text": text}]}
            pending_images.clear()

        try:
            response = await self.provider.chat(
                messages=llm_messages,
                tools=self.tools.get_definitions(master=True),
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

        # Check token usage and compact if approaching context window limit.
        total_tokens = response.usage.get("total_tokens", 0)
        if total_tokens > self.config.context_window * _COMPACT_THRESHOLD:
            logger.info(
                "Session compaction triggered for "
                f"{addr}: token usage {total_tokens}/{self.config.context_window}"
            )
            self._compact_context(session, addr)

        # Extract any <plan> the model wrote and strip it from visible content.
        visible_content, new_plan = self._extract_plan(response.content or "")
        if new_plan:
            logger.debug(f"Plan captured: {new_plan[:120]}")

        # Extract any <image_caption> tags and store them in the media repo.
        visible_content, captions = self._extract_image_captions(visible_content)
        if captions and media_repo:
            for path_from_tag, caption in captions.items():
                media_repo.set_caption(path_from_tag, caption)

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
                    visible_content,
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
            if visible_content:
                await self.bus.publish_outbound(
                    OutboundMessage(address=addr, content=visible_content)
                )
        else:
            final = visible_content or "I've completed processing but have no response to give."
            session.add_message("assistant", final)
            preview = final[:120] + "..." if len(final) > 120 else final
            logger.info(f"Response to {addr}: {preview}")
            await self.bus.publish_outbound(OutboundMessage(address=addr, content=final))

        return new_plan

    async def _address_loop(self, addr: MessageAddress) -> None:
        """
        Event-driven processing loop for a single address.

        Reads AddressEvent (InboundMessage | ToolResultEvent | SystemEvent) from
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
        pending_system_events: list[str] = []
        pending_images: list[str] = []
        current_plan: str | None = None
        session.live_messages = self.context.build_context(
            history=session.get_history(max_messages=self.config.memory_window),
            tools=self.tools,
            channel=addr.channel,
            chat_id=addr.chat_id,
        )

        while True:
            if not tracker.pending:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=False))

            batch = await self.bus.consume_inbound_batch(address=addr)
            needs_llm = False

            # 1. Tool results (in received order; cross-batch hazard impossible since
            #    AgentLoop is unique per address and the queue is FIFO).
            for result in batch.tool_results:
                tracker.handle_result(result, session)
            if batch.tool_results and not tracker.pending:
                for content in pending_system_events:
                    session.live_messages.append({"role": "system", "content": content})
                pending_system_events.clear()
                needs_llm = True

            # 2. System events — buffer if tools are still in flight.
            for event in batch.system_events:
                if tracker.pending:
                    logger.debug(f"SystemEvent buffered (tools in flight): {event.content[:60]}")
                    pending_system_events.append(event.content)
                else:
                    session.live_messages.append({"role": "system", "content": event.content})
                    needs_llm = True

            # 3. User messages — merge into one turn, interrupt any in-flight tools.
            if batch.user_messages:
                await self.bus.publish_outbound(TypingEvent(addr, is_typing=True))
                if tracker.pending:
                    tracker.handle_interrupt(session)
                for content in pending_system_events:
                    session.live_messages.append({"role": "system", "content": content})
                pending_system_events.clear()

                msg = self._merge_user_messages(batch.user_messages)
                preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
                logger.info(f"Processing message from {addr}: {preview}")
                session.add_message(
                    "user",
                    msg.content,
                    sender_id=msg.sender_id,
                    media=msg.media,
                    media_metadata=msg.media_metadata,
                    metadata=msg.metadata,
                )
                pending_images = list(msg.media)
                iteration_count = 0
                needs_llm = True

            if not needs_llm:
                continue

            # Check if the iterations have maxed out.
            if iteration_count >= self.config.max_tool_iterations:
                logger.warning(f"Max tool iterations reached for {addr}")
                continue
            iteration_count += 1

            current_plan = await self._process_llm_turn(
                session,
                tracker,
                call_ctx,
                addr,
                current_plan,
                pending_images=pending_images,
                media_repo=self.media_repo,
            )

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
