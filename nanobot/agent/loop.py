"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.base import ToolContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus import InboundMessage, MessageAddress, MessageBus, OutboundMessage
from nanobot.config import Config
from nanobot.providers.base import LLMProvider
from nanobot.session import SessionManager


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
        self.tools = ToolRegistry(config.tools, master_ctx)

    async def _run_agent_loop(
        self, initial_messages: list[dict], call_ctx: ToolContext
    ) -> tuple[str | None, list[str]]:
        """
        Run the agent iteration loop.

        Args:
            initial_messages: Starting messages for the LLM conversation.
            call_ctx: Per-call context including session address.

        Returns:
            Tuple of (final_content, list_of_tools_used).
        """
        messages = initial_messages
        final_content = None
        tools_used: list[str] = []

        for _ in range(self.config.max_tool_iterations):
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(master=True),
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

                # FUTURE: Dispatch these and lazily wait for responses.
                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments, call_ctx)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                # FUTURE: Break this off into its own file:
                messages.append(
                    {
                        "role": "system",
                        "content": "Process the results and continue computation. If no further processing is required, produce a concluding message for the user or (no message) if one is not required",
                    }
                )
            else:
                final_content = response.content
                break

        return final_content, tools_used

    async def _process_message(
        self, msg: InboundMessage, session_key: MessageAddress | None = None
    ) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.
            session_key: Override session address (used by process_direct).

        Returns:
            The response message, or None if no response needed.
        """
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.address}: {preview}")

        session = self.sessions.get_or_create(msg.address)

        call_ctx = ToolContext(
            workspace=self.tools._master_ctx.workspace,
            bus=self.bus,
            address=msg.address,
        )

        initial_messages = self.context.build_messages(
            history=session.get_history(max_messages=self.config.memory_window),
            current_message=msg.content,
            tools=self.tools,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        final_content, tools_used = await self._run_agent_loop(initial_messages, call_ctx)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")

        session.add_message("user", msg.content)
        session.add_message(
            "assistant", final_content, tools_used=tools_used if tools_used else None
        )
        self.sessions.save(session)

        return OutboundMessage(address=msg.address, content=final_content, metadata=msg.metadata)

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        async with self.sessions:
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
                                    address=msg.address,
                                    content=f"Sorry, I encountered an error: {str(e)}",
                                )
                            )
                    except asyncio.TimeoutError:
                        continue
