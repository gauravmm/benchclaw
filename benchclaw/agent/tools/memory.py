"""Log tool (append-only interaction log)."""

import bisect
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from benchclaw.agent.tools.base import (
    Tool,
    ToolContext,
)
from benchclaw.utils import JsonlIO, _parse_timestamp, now_aware


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
        self._date = now_aware().date()
        self._buffer = JsonlIO.read(self.log_file)
        self._rollover(self._date)
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._buffer = []
        self._date = None

    def _rollover(self, new_date: date) -> None:
        """Move entries 2+ days old to log-<date>.jsonl; keep recent in log.jsonl."""
        cutoff = new_date - timedelta(days=1)
        idx = bisect.bisect_left(
            self._buffer, cutoff, key=lambda e: _parse_timestamp(e["ts"]).date()
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
        now = now_aware()
        if self._buffer:
            assert now >= _parse_timestamp(self._buffer[-1]["ts"]), (
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

    def read_recent(self, n: int = 20) -> str:
        """Return the last n log entries from the in-memory buffer, grouped by date."""
        assert self._date is not None, "LogStore must be used as a context manager"
        recent = self._buffer[-n:]
        if not recent:
            return "(log is empty)"
        lines = []
        current_date = None
        for e in recent:
            ts = _parse_timestamp(e.get("ts", ""))
            d = ts.date()
            if d != current_date:
                lines.append(str(d))
                current_date = d
            lines.append(f"  [{ts.strftime('%H:%M')}] {e.get('content', '')}")
        return "\n".join(lines)

    @staticmethod
    def _fmt(entry: dict) -> str:
        ts = _parse_timestamp(entry.get("ts", ""))
        return f"[{ts.strftime('%Y-%m-%d %H:%M')}] {entry.get('content', '')}"

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
            "Do not log routine prompt-compliance steps such as merely receiving an image or saving a required image annotation. "
            "Frequent entries ensure context survives compaction. Use `search` to regex-search past entries. "
            "Do not tell the user that this log exists or ask them to read it. "
            "Example: `{'action': 'append', 'content': 'Fetched BTC price: $69,420. Set cron for 5m check.'}`."
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
