"""Memory tool: read/write long-term memory and search conversation history."""

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, register_tool
from nanobot.utils import _ensure_dir


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.memory_dir = _ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""


class MemoryTool(Tool):
    """Tool to read/write long-term memory and search conversation history."""

    def __init__(self, workspace: Path):
        self._store = MemoryStore(workspace)

    @property
    def name(self) -> str:
        return "memory"

    @property
    def skill(self) -> str | None:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "Manage persistent memory. Actions: read (load MEMORY.md), "
            "write (overwrite MEMORY.md), search_history (grep HISTORY.md), "
            "append_history (add entry to HISTORY.md)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "search_history", "append_history"],
                    "description": "Action to perform",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write (for write/append_history)",
                },
                "query": {
                    "type": "string",
                    "description": "Search query regex (for search_history)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        content: str = "",
        query: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "read":
            text = self._store.read_long_term()
            return text if text else "(memory is empty)"

        if action == "write":
            if not content:
                return "Error: content is required for write"
            self._store.write_long_term(content)
            return f"Memory updated ({len(content)} chars)"

        if action == "append_history":
            if not content:
                return "Error: content is required for append_history"
            self._store.append_history(content)
            return "History entry appended"

        if action == "search_history":
            if not query:
                return "Error: query is required for search_history"
            history_file = str(self._store.history_file)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "grep",
                    "-i",
                    "-n",
                    query,
                    history_file,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                result = stdout.decode("utf-8", errors="replace").strip()
                return result if result else f"No matches for: {query}"
            except asyncio.TimeoutError:
                return "Error: search timed out"
            except FileNotFoundError:
                return "Error: grep not found"
            except Exception as e:
                return f"Error searching history: {e}"

        return f"Unknown action: {action}"


register_tool("memory", MemoryTool)
