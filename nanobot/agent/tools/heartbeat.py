"""Heartbeat tool — periodic background service that reads HEARTBEAT.md."""

import asyncio
from pathlib import Path
from typing import Any, Callable, Coroutine

from pydantic import BaseModel

from nanobot.agent.tools.base import Tool, register_tool, register_tool_config
from nanobot.heartbeat.service import HeartbeatService


class HeartbeatConfig(BaseModel):
    """Heartbeat service configuration."""

    interval_s: int = 1800  # 30 minutes
    enabled: bool = True


register_tool_config("heartbeat", HeartbeatConfig)


class HeartbeatTool(Tool):
    """Background service that periodically reads HEARTBEAT.md and executes tasks."""

    def __init__(
        self,
        workspace: Path,
        process_direct: Callable[..., Coroutine[Any, Any, str]],
        interval_s: int = 1800,
        enabled: bool = True,
    ):
        self._workspace = workspace
        self._process_direct = process_direct
        self._interval_s = interval_s
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "heartbeat"

    @property
    def description(self) -> str:
        return "Periodic background service that reads HEARTBEAT.md and executes any tasks listed there."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "Heartbeat is a background-only service."

    async def background(self) -> None:
        """Run the heartbeat service until cancelled."""
        if not self._enabled:
            return
        service = HeartbeatService(
            workspace=self._workspace,
            on_heartbeat=self._process_direct,
            interval_s=self._interval_s,
        )
        await service.start()
        try:
            await asyncio.Event().wait()
        finally:
            service.stop()


register_tool("heartbeat", HeartbeatTool)
