"""Memory tool (semantic tagged files) and Log tool (append-only interaction log)."""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, ToolBuildContext, register_tool
from nanobot.utils import _ensure_dir


class MemoryStore:
    """Per-tag YAML memory files in workspace/memory/.

    Each tag is stored as <tag>.yaml with a YAML front matter and freetext body.
    Used by MemoryTool and ContextBuilder.
    """

    def __init__(self, workspace: Path):
        self.memory_dir = _ensure_dir(workspace / "memory")

    def get_available_tags(self) -> list[str]:
        """Return sorted list of existing tag names."""
        return sorted(p.stem for p in self.memory_dir.glob("*.yaml"))

    def read(self, tag: str | None = None) -> str:
        """Read one tag's content or all tags concatenated."""
        if tag:
            path = self.memory_dir / f"{tag}.yaml"
            if not path.exists():
                return f"(no memory for tag '{tag}')"
            return _parse_body(path.read_text(encoding="utf-8"))
        # Read all tags
        parts = []
        for p in sorted(self.memory_dir.glob("*.yaml")):
            body = _parse_body(p.read_text(encoding="utf-8"))
            if body:
                parts.append(f"### {p.stem}\n{body}")
        return "\n\n".join(parts) if parts else "(no memories)"

    def write(self, tag: str, content: str) -> None:
        """Create or update a tagged memory file."""
        path = self.memory_dir / f"{tag}.yaml"
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            created = _parse_front_matter(existing).get("created", now)
        else:
            created = now
        front_matter = f"---\ntag: {tag}\ncreated: {created}\nupdated: {now}\n---\n"
        path.write_text(front_matter + content, encoding="utf-8")

    def get_memory_context(self) -> str:
        """Return available tag names for inclusion in the system prompt."""
        tags = self.get_available_tags()
        if not tags:
            return ""
        return "Available memory tags: " + ", ".join(tags)


def _parse_front_matter(text: str) -> dict[str, str]:
    """Extract key: value pairs from YAML front matter (between --- delimiters)."""
    result: dict[str, str] = {}
    if not text.startswith("---"):
        return result
    end = text.find("---", 3)
    if end == -1:
        return result
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


def _parse_body(text: str) -> str:
    """Extract body text after YAML front matter."""
    if not text.startswith("---"):
        return text
    end = text.find("---", 3)
    if end == -1:
        return text
    return text[end + 3 :].lstrip("\n")


class MemoryTool(Tool):
    """Manage persistent memory with semantic tags."""

    @classmethod
    def build(cls, _config: None, ctx: ToolBuildContext) -> "MemoryTool":
        return cls(workspace=ctx.workspace)

    def __init__(self, workspace: Path):
        self._store = MemoryStore(workspace)

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "Manage persistent memory with semantic tags. "
            "Actions: read (list available tags, or load a specific tag's content), "
            "write (create or update a memory by tag name)."
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
        ctx: ToolBuildContext,
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


class LogStore:
    """Append-only JSONL interaction log in workspace/memory/log.jsonl."""

    def __init__(self, workspace: Path):
        self.log_file = _ensure_dir(workspace / "memory") / "log.jsonl"

    def append(self, content: str) -> str:
        """Append a new entry and return its 8-char ID."""
        entry_id = str(uuid.uuid4())[:8]
        entry = {
            "id": entry_id,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "content": content,
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry_id

    def amend(self, entry_id: str, content: str) -> str:
        """Append an amendment entry referencing entry_id. Returns amendment ID."""
        amendment_id = str(uuid.uuid4())[:8]
        entry = {
            "id": amendment_id,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "amends": entry_id,
            "content": content,
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return amendment_id

    def search(self, query: str) -> str:
        """Regex search across log entries. Returns matching lines."""
        if not self.log_file.exists():
            return "(log is empty)"
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return f"Error: invalid regex: {e}"
        matches = []
        for line in self.log_file.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                if pattern.search(entry.get("content", "")):
                    ts = entry.get("ts", "")
                    eid = entry.get("id", "")
                    amends = f" [amends:{entry['amends']}]" if "amends" in entry else ""
                    matches.append(f"[{ts}] {eid}{amends}: {entry.get('content', '')}")
            except json.JSONDecodeError:
                continue
        return "\n".join(matches) if matches else f"No matches for: {query}"


class LogTool(Tool):
    """Append-only interaction log."""

    @classmethod
    def build(cls, _config: None, ctx: ToolBuildContext) -> "LogTool":
        return cls(workspace=ctx.workspace)

    def __init__(self, workspace: Path):
        self._store = LogStore(workspace)

    @property
    def name(self) -> str:
        return "log"

    @property
    def description(self) -> str:
        return (
            "Append-only interaction log. "
            "**Log interactions that involve changing anything** "
            "(files written, commands run, messages sent, cron jobs created, etc.). "
            "Actions: append (create a timestamped entry, returns entry ID for future amendments), "
            "amend (add a correction or follow-up to a previous entry by ID), "
            "search (regex search across log)."
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
        ctx: ToolBuildContext,
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
