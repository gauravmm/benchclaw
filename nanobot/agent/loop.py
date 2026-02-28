"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.base import ToolBuildContext
from nanobot.agent.tools.memory import MemoryStore
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus import InboundMessage, MessageBus, OutboundMessage
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config import Config


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        provider: LLMProvider,
        session_manager: SessionManager | None = None,
    ):
        self.config = config.agents.master
        self.bus = bus
        self.provider = provider

        self.context = ContextBuilder(config.workspace_path)
        self.sessions = session_manager or SessionManager(config.workspace_path / "sessions")
        # self.subagents = SubagentManager(config=config, provider=provider, bus=bus)

        ctx = ToolBuildContext(
            workspace=config.workspace_path,
            bus=bus,
            process_direct=self.process_direct,
            # subagent_manager=self.subagents,
        )
        self.tools = ToolRegistry.build_all(config.tools, ctx)
        self._running = False

    async def _run_agent_loop(self, initial_messages: list[dict]) -> tuple[str | None, list[str]]:
        """
        Run the agent iteration loop.

        Args:
            initial_messages: Starting messages for the LLM conversation.

        Returns:
            Tuple of (final_content, list_of_tools_used).
        """
        messages = initial_messages
        final_content = None
        tools_used: list[str] = []

        for _ in range(self.config.max_tool_iterations):
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                # TODO: Dispatch these and lazily wait for responses.
                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                # TODO: Break this off into its own file:
                messages.append(
                    {
                        "role": "system",
                        "content": "Process the results and continue computation. If no further processing is required, produce a concluding message for the user.",
                    }
                )
            else:
                final_content = response.content
                break

        return final_content, tools_used

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        async with self.tools:  # starts all tool background() tasks (cron, heartbeat, etc.)
            logger.info("Agent loop started")

            while True:
                try:
                    msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                    try:
                        response = await self._process_message(msg)
                        if response:
                            await self.bus.publish_outbound(response)
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=f"Sorry, I encountered an error: {str(e)}",
                            )
                        )
                except asyncio.TimeoutError:
                    continue

    async def _process_message(
        self, msg: InboundMessage, session_key: str | None = None
    ) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.
            session_key: Override session key (used by process_direct).

        Returns:
            The response message, or None if no response needed.
        """
        # System messages route back via chat_id ("channel:chat_id")
        if msg.channel == "system":
            return await self._process_system_message(msg)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Handle slash commands
        # cmd = msg.content.strip().lower()
        # if cmd == "/new":
        #     # Capture messages before clearing (avoid race condition with background task)
        #     messages_to_archive = session.messages.copy()
        #     session.clear()
        #     self.sessions.save(session)
        #     self.sessions.invalidate(session.key)
        #
        #     async def _consolidate_and_cleanup():
        #         temp_session = Session(key=session.key)
        #         temp_session.messages = messages_to_archive
        #         await self._consolidate_memory(temp_session, archive_all=True)
        #
        #     asyncio.create_task(_consolidate_and_cleanup())
        #     return OutboundMessage(
        #         channel=msg.channel,
        #         chat_id=msg.chat_id,
        #         content="New session started. Memory consolidation in progress.",
        #     )
        # if cmd == "/help":
        #     return OutboundMessage(
        #         channel=msg.channel,
        #         chat_id=msg.chat_id,
        #         content="🐈 nanobot commands:\n/new — Start a new conversation\n/help — Show available commands",
        #     )

        # TODO: Reenable
        # if len(session.messages) > self.memory_window:
        #     asyncio.create_task(self._consolidate_memory(session))

        initial_messages = self.context.build_messages(
            history=session.get_history(max_messages=self.config.memory_window),
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        final_content, tools_used = await self._run_agent_loop(initial_messages)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")

        session.add_message("user", msg.content)
        session.add_message(
            "assistant", final_content, tools_used=tools_used if tools_used else None
        )
        self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata
            or {},  # Pass through for channel-specific needs (e.g. Slack thread_ts)
        )

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")

        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            origin_channel, origin_chat_id = msg.chat_id.split(":", 1)
        else:
            origin_channel, origin_chat_id = "cli", msg.chat_id

        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        initial_messages = self.context.build_messages(
            history=session.get_history(max_messages=self.memory_window),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        final_content, _ = await self._run_agent_loop(initial_messages)

        if final_content is None:
            final_content = "Background task completed."

        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)

        return OutboundMessage(
            channel=origin_channel, chat_id=origin_chat_id, content=final_content
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).

        Args:
            content: The message content.
            session_key: Session identifier (overrides channel:chat_id for session lookup).
            channel: Source channel (for tool context routing).
            chat_id: Source chat ID (for tool context routing).

        Returns:
            The agent's response.
        """
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)

        response = await self._process_message(msg, session_key=session_key)
        return response.content if response else ""
