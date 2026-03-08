"""Memory tool (semantic tagged files) and Log tool (append-only interaction log)."""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from benchclaw.agent.tools.base import Tool, ToolContext, register_tool
from benchclaw.utils import _ensure_dir


class MemoryStore:
    """Per-tag plain-text memory files in workspace/memory/.

    Each tag is stored as <tag>.txt.
    Used by MemoryTool and ContextBuilder.
    """

    def __init__(self, workspace: Path):
        self.memory_dir = _ensure_dir(workspace / "memory")

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
                return "Error: tag is required for write"
            if not content:
                return "Error: content is required for write"
            self._store.write(tag, content)
            return f"Memory '{tag}' updated"

        return f"Unknown action: {action}"


_LOG_RETENTION_DAYS = 14


class LogStore:
    """Append-only JSONL interaction log in workspace/logs/<date>.jsonl.

    One file per calendar day (local time). On startup and whenever a new
    daily file is first created, files older than LOG_RETENTION_DAYS are
    deleted.
    """

    def __init__(self, workspace: Path):
        self.logs_dir = _ensure_dir(workspace / "logs")
        self._rotate(datetime.now().astimezone())

    def _log_file(self, ts: datetime) -> Path:
        return self.logs_dir / f"{ts.date()}.jsonl"

    def _rotate(self, ts: datetime) -> None:
        """Delete log files older than _LOG_RETENTION_DAYS relative to ts."""
        cutoff = ts.date().toordinal() - _LOG_RETENTION_DAYS
        for p in self.logs_dir.glob("*.jsonl"):
            try:
                if datetime.strptime(p.stem, "%Y-%m-%d").toordinal() < cutoff:
                    p.unlink()
            except ValueError:
                pass

    def _write_entry(self, entry: dict) -> None:
        ts = datetime.fromisoformat(entry["ts"])
        log_file = self._log_file(ts)
        is_new = not log_file.exists()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if is_new:
            self._rotate(ts)

    def append(self, content: str) -> str:
        """Append a new entry and return its 8-char ID."""
        entry_id = str(uuid.uuid4())[:8]
        self._write_entry(
            {
                "id": entry_id,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "content": content,
            }
        )
        return entry_id

    def amend(self, entry_id: str, content: str) -> str:
        """Append an amendment entry referencing entry_id. Returns amendment ID."""
        amendment_id = str(uuid.uuid4())[:8]
        self._write_entry(
            {
                "id": amendment_id,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "amends": entry_id,
                "content": content,
            }
        )
        return amendment_id

    def _iter_entries(self, files: list[Path]):
        for p in files:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    @staticmethod
    def _fmt(entry: dict) -> str:
        amends = f" [amends:{entry['amends']}]" if "amends" in entry else ""
        return f"[{entry.get('ts', '')}] {entry.get('id', '')}{amends}: {entry.get('content', '')}"

    def read_recent(self, n: int = 20) -> str:
        """Return the last n log entries from today's file."""
        log_file = self._log_file(datetime.now().astimezone())
        if not log_file.exists():
            return "(log is empty)"
        lines = [self._fmt(e) for e in self._iter_entries([log_file])]
        return "\n".join(lines[-n:]) if lines else "(log is empty)"

    def search(self, query: str) -> str:
        """Regex search across all retained log files. Returns matching lines."""
        files = sorted(self.logs_dir.glob("*.jsonl"))
        if not files:
            return "(log is empty)"
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return f"Error: invalid regex: {e}"
        matches = [
            self._fmt(e) for e in self._iter_entries(files) if pattern.search(e.get("content", ""))
        ]
        return "\n".join(matches) if matches else f"No matches for: {query}"


class LogTool(Tool):
    """Append-only interaction log."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "LogTool":
        return cls(workspace=ctx.workspace)

    def __init__(self, workspace: Path):
        self._store = LogStore(workspace)

    @property
    def name(self) -> str:
        return "log"

    @property
    def description(self) -> str:
        return (
            "Append-only timestamped log for recording every action that changes state (files written, commands run, messages sent, cron jobs created). "
            "Use `append` to add a new entry (returns an 8-char ID), `amend` to attach a correction to a prior entry by ID, or `search` to regex-search past entries. "
            "This tool is for recording what the agent did and why, for later session compaction. "
            "Do not tell the user that this log exists or ask them to read it; it's for the agent's internal use only. "
            "Example: `{'action': 'append', 'content': 'Edited config.yaml to add Telegram token'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["append", "amend", "search"],
                    "description": "Action to perform",
                },
                "content": {
                    "type": "string",
                    "description": "Log entry content (for append/amend)",
                },
                "entry_id": {
                    "type": "string",
                    "description": "ID of entry to amend (for amend)",
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
        entry_id: str = "",
        query: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "append":
            if not content:
                return "Error: content is required for append"
            eid = self._store.append(content)
            return f"Logged (id: {eid})"

        if action == "amend":
            if not entry_id:
                return "Error: entry_id is required for amend"
            if not content:
                return "Error: content is required for amend"
            aid = self._store.amend(entry_id, content)
            return f"Amendment logged (id: {aid})"

        if action == "search":
            if not query:
                return "Error: query is required for search"
            return self._store.search(query)

        return f"Unknown action: {action}"


register_tool("memory", MemoryTool)
register_tool("log", LogTool)
