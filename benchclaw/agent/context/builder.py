"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader

from benchclaw.agent.skills import SkillsLoader
from benchclaw.agent.tools.memory import MemoryStore
from benchclaw.agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from benchclaw.agent.tools.base import Tool

BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md"]


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self._jinja = Environment(
            loader=PackageLoader("benchclaw.agent.context", "templates"),
            keep_trailing_newline=True,
        )

    def build_system_prompt(
        self,
        tools: Iterable["Tool"] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> str:
        system = platform.system()
        bootstrap_files = [
            {"name": f, "content": (self.workspace / f).read_text(encoding="utf-8")}
            for f in BOOTSTRAP_FILES
            if (self.workspace / f).exists()
        ]
        all_skills = self.skills.get_all_skills()
        return self._jinja.get_template("system_prompt.j2").render(
            now=datetime.now().strftime("%Y-%m-%d %H:%M (%A)"),
            runtime=f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}",
            workspace_path=str(self.workspace.expanduser().resolve()),
            bootstrap_files=bootstrap_files,
            memory=self.memory.get_memory_context() or "",
            skills=all_skills,
            tools=[{"name": t.name, "description": t.description} for t in (tools or [])],
            channel=channel,
            chat_id=chat_id,
        )

    def build_context(
        self,
        history: list[dict[str, Any]],
        tools: ToolRegistry | None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the base context: system prompt followed by history. No user message appended."""
        return [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    tools.values() if tools else None, channel, chat_id
                ),
            },
            *history,
        ]

    def user_message(self, content: str, media: list[str] | None = None) -> dict[str, Any]:
        return {"role": "user", "content": self._build_user_content(content, media)}

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def tool_result(self, tool_call_id: str, tool_name: str, result: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result}

    def assistant_message(
        self,
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}

        if tool_calls:
            msg["tool_calls"] = tool_calls

        # Thinking models reject history without this
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content

        return msg


if __name__ == "__main__":
    import asyncio

    from benchclaw.agent.tools.base import ToolContext
    from benchclaw.agent.tools.registry import ToolRegistry
    from benchclaw.config import ConfigManager

    async def main() -> None:
        with ConfigManager() as config:
            ctx = ToolContext(workspace=config.workspace_path)
            async with ToolRegistry(config.tools, ctx) as tools:
                print(ContextBuilder(config.workspace_path).build_system_prompt(tools.values()))

    asyncio.run(main())
