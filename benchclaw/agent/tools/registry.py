"""Tool registry: manages tool lifecycle and execution."""

import asyncio
import contextlib
from collections.abc import Iterable
from typing import Any, Self

from benchclaw.agent.tools.base import (
    _TOOL_REGISTRY,
    ParsedInnerTag,
    Tool,
    ToolContext,
)
from benchclaw.agent.tools.mcp_manager import MCPManager


class ToolRegistry:
    """
    Registry for agent tools.

    Manages tool construction, lifecycle (background tasks and async context
    managers), and execution. Enter as an async context manager to start all
    tool background() tasks and enter any tool async context managers.
    Raises RuntimeError if entered more than once on the same instance.
    """

    def __init__(self, tools_config: Any, ctx: ToolContext, mcp_manager: MCPManager | None = None):
        self._tools: dict[str, Tool] = {}
        self._inner_tag_tools: dict[str, Tool] = {}
        self._master_ctx = ctx
        self._mcp_manager = mcp_manager
        self._running = False
        self._exit_stack = contextlib.AsyncExitStack()

        for name, tool_cls in _TOOL_REGISTRY.items():
            if ctx.is_subagent and tool_cls.master_only:
                continue  # skip master-only tools in subagent context
            tool = tool_cls.build(getattr(tools_config, name, None), ctx)
            self._tools[tool.name] = tool
            if spec := tool.inner_tag:
                if spec.name in self._inner_tag_tools:
                    other = self._inner_tag_tools[spec.name]
                    raise RuntimeError(
                        f"Duplicate inner tag '{spec.name}' for tools "
                        f"'{other.name}' and '{tool.name}'"
                    )
                self._inner_tag_tools[spec.name] = tool

    async def __aenter__(self) -> Self:
        if self._running:
            raise RuntimeError(
                "ToolRegistry is already running; cannot enter the same instance twice"
            )
        self._running = True
        await self._exit_stack.__aenter__()
        for tool in self._tools.values():
            if hasattr(tool, "__aenter__"):
                await self._exit_stack.enter_async_context(tool)  # type: ignore[arg-type]
            if type(tool).background is not Tool.background:
                tool._task = asyncio.create_task(tool.background(self._master_ctx), name=tool.name)
        if self._mcp_manager:
            await self._exit_stack.enter_async_context(self._mcp_manager)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for tool in self._tools.values():
            if tool._task:
                tool._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(tool._task), timeout=5.0)
                tool._task = None
        await self._exit_stack.__aexit__(*exc_info)
        self._running = False

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
        defs = [tool.to_schema() for tool in self.values(master=master)]
        if self._mcp_manager:
            defs.extend(self._mcp_manager.get_definitions())
        return defs

    def get_inner_tag_tools(self) -> dict[str, Tool]:
        """Return tools keyed by their private inline tag name."""
        return dict(self._inner_tag_tools)

    async def execute(self, name: str, params: dict[str, Any], ctx: ToolContext) -> str:
        """Execute a tool by name with given parameters and call context."""
        if self._mcp_manager and name in self._mcp_manager:
            try:
                return await self._mcp_manager.execute(name, params)
            except Exception as e:
                return f"Error executing MCP tool '{name}': {e}"
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

    async def execute_inner_tag(
        self, tag_name: str, parsed: ParsedInnerTag, ctx: ToolContext
    ) -> str | list[dict[str, Any]] | None:
        """Dispatch one parsed private tag to the owning tool."""
        tool = self._inner_tag_tools.get(tag_name)
        if not tool:
            raise ValueError(f"No tool registered for inner tag '{tag_name}'")
        return await tool.execute_inner_tag(ctx, parsed)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools or (self._mcp_manager is not None and name in self._mcp_manager)
