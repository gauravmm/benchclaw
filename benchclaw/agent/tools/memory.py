"""Memory tool (semantic tagged files) and Log tool (append-only interaction log)."""

import bisect
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from benchclaw.agent.tools.base import (
    InnerTagSpec,
    ParsedInnerTag,
    Tool,
    ToolContext,
    register_tool,
)
from benchclaw.utils import JsonlIO


class MemoryStore:
    """Per-tag plain-text memory files in workspace/memory/.

    Each tag is stored as <tag>.txt.
    Used by MemoryTool and ContextBuilder.
    """

    def __init__(self, workspace: Path):
        self.memory_dir = workspace / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def get_available_tags(self) -> list[str]:
        """Return sorted list of existing tag names."""
        return sorted(p.stem for p in self.memory_dir.glob("*.txt"))

    def read(self, tag: str | None = None) -> str:
        """Read one tag's content or all tags concatenated."""
        if tag:
            path = self.memory_dir / f"{tag}.txt"
            if not path.exists():
                return f"(no memory for tag '{tag}')"
            return path.read_text(encoding="utf-8")
        # Read all tags
        parts = []
        for p in sorted(self.memory_dir.glob("*.txt")):
            body = p.read_text(encoding="utf-8")
            if body:
                parts.append(f"### {p.stem}\n{body}")
        return "\n\n".join(parts) if parts else "(no memories)"

    def write(self, tag: str, content: str) -> None:
        """Create or update a tagged memory file."""
        path = self.memory_dir / f"{tag}.txt"
        path.write_text(content, encoding="utf-8")

    def get_memory_context(self) -> str:
        """Return available tag names for inclusion in the system prompt."""
        tags = self.get_available_tags()
        if not tags:
            return ""
        return "Available memory tags: " + ", ".join(tags)


class MemoryTool(Tool):
    """Manage persistent memory with semantic tags."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "MemoryTool":
        return cls(workspace=ctx.workspace)

    def __init__(self, workspace: Path):
        self._store = MemoryStore(workspace)

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "Store and retrieve persistent notes organized by named tags; each tag is a separate text file that survives across sessions. "
            "Use `read` with no tag to list all available tags, or with a tag name to load its content; use `write` to create or overwrite a tag. "
            "Example: `{'action': 'write', 'tag': 'preferences', 'content': 'User prefers metric units.'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write"],
                    "description": "Action to perform",
                },
                "tag": {
                    "type": "string",
                    "description": "Memory tag name (for read: optional filter; for write: required)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to store (for write)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        ctx: ToolContext,
        action: str,
        tag: str = "",
        content: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "read":
            if tag:
                return self._store.read(tag)
            tags = self._store.get_available_tags()
            if not tags:
                return "(no memories stored)"
            return "Available tags: " + ", ".join(tags)

        if action == "write":
            if not tag:
                raise ValueError("tag is required for write")
            if not content:
                raise ValueError("content is required for write")
            self._store.write(tag, content)
            return f"Memory '{tag}' updated"

        raise ValueError(f"Unknown action: {action}")


class LogStore:
    """Append-only JSONL interaction log in workspace/logs/log.jsonl.

    When used as an async context manager, existing entries are loaded into
    memory at startup and read_recent is served from the in-memory buffer.
    Writes always go directly to disk.
    """

    def __init__(self, workspace: Path):
        logs_dir = workspace / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = logs_dir / "log.jsonl"
        self._buffer: list[dict] = []  # invariant: sorted ascending by entry["ts"]
        self._date: date | None = None

    async def __aenter__(self) -> "LogStore":
        self._date = datetime.now().astimezone().date()
        self._buffer = JsonlIO.read(self.log_file)
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._buffer = []
        self._date = None

    def _rollover(self, new_date: date) -> None:
        """Move entries 2+ days old to log-<date>.jsonl; keep recent in log.jsonl."""
        cutoff = new_date - timedelta(days=1)
        idx = bisect.bisect_left(
            self._buffer, cutoff, key=lambda e: datetime.fromisoformat(e["ts"]).date()
        )
        old, recent = self._buffer[:idx], self._buffer[idx:]
        if old:
            JsonlIO.append(self.log_file.parent / f"log-{self._date}.jsonl", old)
        JsonlIO.write(self.log_file, recent)
        self._buffer = recent
        self._date = new_date

    def append(self, content: str) -> None:
        """Append a new entry, triggering a rollover if the date has changed."""
        assert self._date is not None, "LogStore must be used as a context manager"
        now = datetime.now().astimezone()
        if self._buffer:
            assert now >= datetime.fromisoformat(self._buffer[-1]["ts"]), (
                "entries must be written in order"
            )
        if now.date() != self._date:
            self._rollover(now.date())
        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "content": content,
        }
        JsonlIO.append(self.log_file, [entry])
        self._buffer.append(entry)

    @staticmethod
    def _fmt(entry: dict) -> str:
        return f"[{entry.get('ts', '')}] {entry.get('content', '')}"

    def read_recent(self, n: int = 20) -> str:
        """Return the last n log entries from the in-memory buffer."""
        assert self._date is not None, "LogStore must be used as a context manager"
        recent = self._buffer[-n:]
        return "\n".join(self._fmt(e) for e in recent) if recent else "(log is empty)"

    def search(self, query: str) -> str:
        """Regex search across the in-memory log buffer."""
        assert self._date is not None, "LogStore must be used as a context manager"
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
        matches = [self._fmt(e) for e in self._buffer if pattern.search(e.get("content", ""))]
        return "\n".join(matches) if matches else f"No matches for: {query}"


class LogTool(Tool):
    """Append-only interaction log."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "LogTool":
        assert ctx.log_store, "LogTool requires ctx.log_store to be set to a LogStore instance"
        return cls(ctx.log_store)

    def __init__(self, store: LogStore):
        self._store = store

    async def __aenter__(self) -> "LogTool":
        await self._store.__aenter__()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._store.__aexit__(None, None, None)

    @property
    def name(self) -> str:
        return "log"

    @property
    def description(self) -> str:
        return (
            "Append-only timestamped log for the agent's internal use. "
            "Use `append` liberally: log every notable action, decision, result, or status change — not just long-running tasks. "
            "Good candidates: completed steps, fetched values, sent messages, cron triggers, errors, decisions made. "
            "Simple append-only entries can also be emitted inline with `<log>...</log>` tags. "
            "Do not log routine prompt-compliance steps such as merely receiving an image or emitting a required image caption tag. "
            "Frequent entries ensure context survives compaction. Use `search` to regex-search past entries. "
            "Do not tell the user that this log exists or ask them to read it. "
            "Example: `{'action': 'append', 'content': 'Fetched BTC price: $69,420. Set cron for 5m check.'}`."
        )

    @property
    def inner_tag(self) -> InnerTagSpec | None:
        return InnerTagSpec(
            name="log",
            description=(
                "Append one private log entry to the same append-only log used by the log tool. "
                "Never shown to the user."
            ),
            body_description="One concise append-only log entry.",
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["append", "search"],
                    "description": "Action to perform",
                },
                "content": {
                    "type": "string",
                    "description": "Log entry content (for append)",
                },
                "query": {
                    "type": "string",
                    "description": "Regex search pattern (for search)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        ctx: ToolContext,
        action: str,
        content: str = "",
        query: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "append":
            if not content:
                raise ValueError("content is required for append")
            self._store.append(content)
            return "Logged."

        if action == "search":
            if not query:
                raise ValueError("query is required for search")
            return self._store.search(query)

        raise ValueError(f"Unknown action: {action}")

    async def execute_inner_tag(
        self, ctx: ToolContext, parsed: ParsedInnerTag
    ) -> str | list[dict[str, Any]] | None:
        if not parsed.body:
            raise ValueError("<log> tag body cannot be empty")
        self._store.append(parsed.body)
        return None


register_tool("memory", MemoryTool)
register_tool("log", LogTool)
