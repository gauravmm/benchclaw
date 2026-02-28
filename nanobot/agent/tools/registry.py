"""Tool registry for dynamic tool management."""

from contextlib import AsyncExitStack
from typing import Any, Iterable, Self

from nanobot.agent.tools.base import _TOOL_REGISTRY, Tool, ToolBuildContext


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    Enter as an async context manager to start all tool background() tasks.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for tool in self._tools.values():
            await self._stack.enter_async_context(tool)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack:
            await self._stack.__aexit__(*exc_info)
            self._stack = None

    @classmethod
    def build_all(cls, tools_config: Any, ctx: ToolBuildContext) -> Self:
        """Build a ToolRegistry from all registered tools using the given config and context."""
        registry = cls()
        for name, tool_cls in _TOOL_REGISTRY.items():
            if ctx.is_subagent and tool_cls.master_only:
                continue  # skip agent-only tools in subagent context (no bus)
            registry.register(tool_cls.build(getattr(tools_config, name, None), ctx))
        return registry

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def values(self) -> Iterable[Tool]:
        """Iterate over all registered tools."""
        return self._tools.values()

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """
        Execute a tool by name with given parameters.

        Args:
            name: Tool name.
            params: Tool parameters.

        Returns:
            Tool execution result as string.

        Raises:
            KeyError: If tool not found.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"

        try:
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            return await tool.execute(**params)
        except Exception as e:
            return f"Error executing {name}: {str(e)}"

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
