"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader

from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.memory import MemoryStore

if TYPE_CHECKING:
    from nanobot.agent.tools.base import Tool

BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self._jinja = Environment(
            loader=PackageLoader("nanobot.agent.context", "templates"),
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
        tool_data = [
            {
                "name": t.name,
                "description": t.description,
            }
            for t in (tools or [])
        ]
        return self._jinja.get_template("system_prompt.j2").render(
            now=datetime.now().strftime("%Y-%m-%d %H:%M (%A)"),
            runtime=f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}",
            workspace_path=str(self.workspace.expanduser().resolve()),
            bootstrap_files=bootstrap_files,
            memory=self.memory.get_memory_context() or "",
            skills=self.skills.get_all_skills(),
            tools=tool_data,
            channel=channel,
            chat_id=chat_id,
        )

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        tools: Iterable["Tool"] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        messages = []

        messages.append(
            {"role": "system", "content": self.build_system_prompt(tools, channel, chat_id)}
        )

        # History
        messages.extend(history)

        # Current message (with optional image attachments)
        user_content = self._build_user_content(current_message, media)
        messages.append({"role": "user", "content": user_content})

        return messages

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

    def add_tool_result(
        self, messages: list[dict[str, Any]], tool_call_id: str, tool_name: str, result: str
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.

        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.

        Returns:
            Updated message list.
        """
        messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result}
        )
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.

        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
            reasoning_content: Thinking output (Kimi, DeepSeek-R1, etc.).

        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}

        if tool_calls:
            msg["tool_calls"] = tool_calls

        # Thinking models reject history without this
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content

        messages.append(msg)
        return messages


if __name__ == "__main__":
    from nanobot.config import ConfigManager

    with ConfigManager() as config:
        print(ContextBuilder(config.workspace_path).build_system_prompt())
