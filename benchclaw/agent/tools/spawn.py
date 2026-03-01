"""Spawn tool for creating background subagents."""

from typing import TYPE_CHECKING, Any

from benchclaw.agent.tools.base import Tool, ToolContext, register_tool

if TYPE_CHECKING:
    from benchclaw.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """
    Tool to spawn a subagent for background task execution.

    The subagent runs asynchronously and announces its result back
    to the main agent when complete.
    """

    master_only = True

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "SpawnTool":
        return cls(manager=ctx.subagent_manager)

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str | None:
        return (
            "Spawn a subagent to execute a long-running or complex task asynchronously so the main agent can return immediately. "
            "The subagent has access to filesystem, shell, and web tools but cannot send messages or spawn further subagents. "
            "Example: `{'task': 'Scrape the top 10 Hacker News links and save them to workspace/hn.md', 'label': 'HN scraper'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (for display)",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self, ctx: ToolContext, task: str, label: str | None = None, **kwargs: Any
    ) -> str:
        """Spawn a subagent to execute the given task."""
        return await self._manager.spawn(
            task=task,
            label=label,
            origin=ctx.address,
        )


register_tool("spawn", SpawnTool)
