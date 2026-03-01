"""Subagent manager for background task execution."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.base import ToolContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus import InboundMessage, MessageAddress, MessageBus
from nanobot.providers.base import LLMProvider

if TYPE_CHECKING:
    from nanobot.config import Config


class SubagentManager:
    """
    Manages background subagent execution.

    Subagents are lightweight agent instances that run in the background
    to handle specific tasks. They share the same LLM provider but have
    isolated context and a focused system prompt.
    """

    def __init__(
        self,
        config: Config,
        provider: LLMProvider,
        bus: MessageBus,
    ):
        self._config = config
        self.provider = provider
        self.bus = bus
        self.workspace = config.workspace_path
        self.model = config.agents.master.model
        self.temperature = config.agents.master.temperature
        self.max_tokens = config.agents.master.max_tokens
        self._running_tasks: dict[str, asyncio.Task[None]] = {}

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin: MessageAddress | None = None,
    ) -> str:
        """
        Spawn a subagent to execute a task in the background.

        Args:
            task: The task description for the subagent.
            label: Optional human-readable label for the task.
            origin: The address to announce results to.

        Returns:
            Status message indicating the subagent was started.
        """
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        # Create background task
        bg_task = asyncio.create_task(self._run_subagent(task_id, task, display_label, origin))
        self._running_tasks[task_id] = bg_task

        # Cleanup when done
        bg_task.add_done_callback(lambda _: self._running_tasks.pop(task_id, None))

        logger.info(f"Spawned subagent [{task_id}]: {display_label}")
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: MessageAddress | None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info(f"Subagent [{task_id}] starting task: {label}")

        try:
            build_ctx = ToolContext(
                workspace=self.workspace,
                is_subagent=True,
                # No bus/subagent_manager → master_only tools are excluded
            )
            # Per-call context for subagent tool executions (no session address)
            call_ctx = ToolContext(
                workspace=self.workspace,
                is_subagent=True,
            )
            # TODO: Remove this call
            async with ToolRegistry(self._config.tools, build_ctx) as tools:
                # Build messages with subagent-specific prompt
                system_prompt = self._build_subagent_prompt(task)
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ]

                # Run agent loop (limited iterations)
                max_iterations = 15
                iteration = 0
                final_result: str | None = None

                while iteration < max_iterations:
                    iteration += 1

                    response = await self.provider.chat(
                        messages=messages,
                        tools=tools.get_definitions(master=False),
                        model=self.model,
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                    )

                    if response.has_tool_calls:
                        tool_call_dicts = [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in response.tool_calls
                        ]
                        messages.append(
                            {
                                "role": "assistant",
                                "content": response.content or "",
                                "tool_calls": tool_call_dicts,
                            }
                        )

                        for tool_call in response.tool_calls:
                            args_str = json.dumps(tool_call.arguments)
                            logger.debug(
                                f"Subagent [{task_id}] executing: {tool_call.name} with arguments: {args_str}"
                            )
                            result = await tools.execute(
                                tool_call.name, tool_call.arguments, call_ctx
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": tool_call.name,
                                    "content": result,
                                }
                            )
                    else:
                        final_result = response.content
                        break

            if final_result is None:
                final_result = "Task completed but no final response was generated."

            logger.info(f"Subagent [{task_id}] completed successfully")
            await self._announce_result(task_id, label, task, final_result, origin, "ok")

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error(f"Subagent [{task_id}] failed: {e}")
            await self._announce_result(task_id, label, task, error_msg, origin, "error")

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: MessageAddress | None,
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        # Inject as system message to trigger main agent
        # chat_id encodes the origin address for routing back
        origin_chat_id = f"{origin.channel}:{origin.chat_id}" if origin else "cli:direct"
        msg = InboundMessage(
            address=MessageAddress(channel="system", chat_id=origin_chat_id),
            sender_id="subagent",
            content=announce_content,
        )

        await self.bus.publish_inbound(msg)
        logger.debug(f"Subagent [{task_id}] announced result to {origin_chat_id}")

    def _build_subagent_prompt(self, task: str) -> str:
        """Build a focused system prompt for the subagent."""
        import time as _time
        from datetime import datetime

        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"

        return f"""# Subagent

## Current Time
{now} ({tz})

You are a subagent spawned by the main agent to complete a specific task.

## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {self.workspace}
Skills are available at: {self.workspace}/skills/ (read SKILL.md files as needed)

When you have completed the task, provide a clear summary of your findings or actions."""

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
