"""Session management for conversation history."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pathvalidate import sanitize_filename

from benchclaw.bus import MessageAddress

MAX_SESSIONS = 50


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    """

    addr: MessageAddress
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    # TODO: When this hits a threshold, log a summary and continue from that point.
    last_consolidated: int = 0
    # In-memory LLM context for the current run; not persisted.
    live_messages: list[dict[str, Any]] = field(default_factory=list)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            **kwargs,
        }
        self.history.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        """Get recent messages in LLM format (role + content only)."""
        return [{"role": m["role"], "content": m["content"]} for m in self.history[-max_messages:]]

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.history = []
        self.live_messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()

    @classmethod
    def load(cls, path: Path) -> "Session | None":
        """Load a session from a JSONL file. Returns None if file missing or invalid."""
        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0
            addr: MessageAddress | None = None

            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = (
                            datetime.fromisoformat(data["created_at"])
                            if data.get("created_at")
                            else None
                        )
                        last_consolidated = data.get("last_consolidated", 0)
                        if data.get("address"):
                            addr = MessageAddress.from_string(data["address"])
                    else:
                        messages.append(data)

            if addr is None:
                logger.warning(f"No address in session file {path}, skipping")
                return None

            return cls(
                addr=addr,
                history=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
            )
        except Exception as e:
            logger.warning(f"Failed to load session from {path}: {e}")
            return None

    def save(self, path: Path) -> None:
        """Save this session to a JSONL file."""
        with open(path, "w") as f:
            metadata_line = {
                "_type": "metadata",
                "address": str(self.addr),
                "created_at": self.created_at.isoformat(timespec="seconds"),
                "updated_at": self.updated_at.isoformat(timespec="seconds"),
                "metadata": self.metadata,
                "last_consolidated": self.last_consolidated,
            }
            f.write(json.dumps(metadata_line) + "\n")
            for msg in self.history:
                f.write(json.dumps(msg) + "\n")


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    Use as an async context manager: all sessions are loaded on enter and flushed on exit.
    Individual sessions can be saved mid-session via save() for durability.
    """

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self._archive_dir = sessions_dir / ".archive"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[MessageAddress, Session] = {}

    def _get_session_path(self, key: MessageAddress) -> Path:
        return self.sessions_dir / f"{sanitize_filename(str(key))}.jsonl"

    async def __aenter__(self) -> "SessionManager":
        """Load all sessions from disk, enforcing the MAX_SESSIONS limit."""
        sessions: list[Session] = []
        for path in self.sessions_dir.glob("*.jsonl"):
            if (session := Session.load(path)) is not None:
                sessions.append(session)

        if len(sessions) > MAX_SESSIONS:
            sessions.sort(key=lambda s: s.updated_at, reverse=True)
            for old_session in sessions[MAX_SESSIONS:]:
                self._archive(old_session)
            sessions = sessions[MAX_SESSIONS:]

        self._cache = {s.addr: s for s in sessions}
        return self

    async def __aexit__(self, *_: Any) -> None:
        """Write all cached sessions to disk."""
        for session in self._cache.values():
            session.save(self._get_session_path(session.addr))

    def _archive(self, s: Session) -> None:
        """Move a session file to the archive directory with a timestamp suffix."""
        path = self._get_session_path(s.addr)
        archive_path = (
            self._archive_dir
            / f"{path.stem}_{datetime.now().strftime('%Y%m%dT%H%M%S')}{path.suffix}"
        )

        self._archive_dir.mkdir(parents=True, exist_ok=True)
        s.save(archive_path)  # Save the latest state to the archive
        path.unlink(missing_ok=True)  # Remove the original file.

    def get(self, key: MessageAddress) -> Session:
        """Get an existing session or create a new one."""
        if key not in self._cache:
            self._cache[key] = Session(addr=key)
            if len(self._cache) > MAX_SESSIONS:
                oldest = min(
                    (s for s in self._cache.values() if s.addr != key),
                    key=lambda s: s.updated_at,
                )
                self._archive(oldest)
                del self._cache[oldest.addr]
        return self._cache[key]

    def clear(self, key: MessageAddress) -> None:
        """Remove a session from the in-memory cache and archive it on disk."""
        if s := self._cache.pop(key, None):
            self._archive(s)
