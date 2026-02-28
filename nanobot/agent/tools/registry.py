"""Tool registry: manages tool lifecycle and execution."""

import asyncio
import contextlib
from collections.abc import Iterable
from typing import Any

from nanobot.agent.tools.base import (
    _TOOL_REGISTRY,
    Tool,
    ToolBuildContext,
)


class ToolRegistry:
    """
    Registry for agent tools.

    Manages tool construction, lifecycle (background tasks), and execution.
    Enter as an async context manager to start all tool background() tasks.
    Raises RuntimeError if entered more than once on the same instance.
    """

    def __init__(self, tools_config: Any, ctx: ToolBuildContext):
        self._tools: dict[str, Tool] = {}
        self._master_ctx = ctx
        self._running = False

        for name, tool_cls in _TOOL_REGISTRY.items():
            if ctx.is_subagent and tool_cls.master_only:
                continue  # skip master-only tools in subagent context
            tool = tool_cls.build(getattr(tools_config, name, None), ctx)
            self._tools[tool.name] = tool

    async def __aenter__(self) -> "ToolRegistry":
        if self._running:
            raise RuntimeError(
                "ToolRegistry is already running; cannot enter the same instance twice"
            )
        self._running = True
        for tool in self._tools.values():
            tool._task = asyncio.create_task(tool.background(self._master_ctx), name=tool.name)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for tool in self._tools.values():
            if tool._task:
                tool._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(tool._task), timeout=5.0)
                tool._task = None
        self._running = False

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def values(self, master: bool = True) -> Iterable[Tool]:
        """Iterate over registered tools.

        Args:
            master: If True, return all tools. If False, exclude master_only tools.
        """
        for tool in self._tools.values():
            if not master and tool.master_only:
                continue
            yield tool

    def get_definitions(self, master: bool = True) -> list[dict[str, Any]]:
        """Get tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self.values(master=master)]

    async def execute(self, name: str, params: dict[str, Any], ctx: ToolBuildContext) -> str:
        """Execute a tool by name with given parameters and call context."""
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"
        try:
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            return await tool.execute(ctx, **params)
        except Exception as e:
            return f"Error executing {name}: {str(e)}"

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
