"""Session management for conversation history."""

import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, TypedDict, Unpack

from loguru import logger
from pathvalidate import sanitize_filename

from benchclaw.bus import MediaMetadata, MessageAddress, ToolResult
from benchclaw.utils import _parse_timestamp, ensure_aware, now_aware

MAX_SESSIONS = 50
_MAX_REASONING_CHARS = 500

EventKind = Literal["user", "assistant", "tool", "system", "summary"]

# Persistence-only marker kinds; not part of ConversationEvent.
ClearAction = Literal["reset", "forget"]


@dataclass(frozen=True)
class RenderOptions:
    include_reasoning: bool = True
    pending_media_paths: list[str] | None = None
    max_inline_image_url_chars: int | None = None


class MediaRenderer(Protocol):
    def build_media_blocks(self, paths: list[str]) -> list[dict[str, object]]: ...


def _sender_label(metadata: dict[str, Any]) -> str | None:
    """Return a channel-provided display label for a sender, if present."""
    label = metadata.get("sender_label")
    return str(label) if label else None


def _format_prefix_time(sent_at: datetime | None) -> str | None:
    """Convert timestamp to HH:MM for compact user prefixes."""
    with suppress(ValueError, TypeError):
        if sent_at:
            return ensure_aware(sent_at).strftime("%H:%M")
    return None


def _user_prefix(sender: str | None, sent_at: datetime | None) -> str | None:
    """Build a user message prefix containing sender and/or timestamp."""
    short_time = _format_prefix_time(sent_at)
    if sender and short_time:
        return f"{sender} @{short_time}"
    if sender:
        return sender
    if short_time:
        return f"@{short_time}"
    return None


def _channel_display_name(channel: str) -> str:
    """Return a readable channel label for prompts."""
    known = {
        "telegram": "Telegram",
        "whatsapp": "WhatsApp",
        "email": "Email",
    }
    if channel in known:
        return known[channel]
    return channel.replace("_", " ").title()


def _render_user_content(
    content: str,
    *,
    media: list[str] | None = None,
    sender: str | None = None,
    sent_at: datetime | None = None,
) -> str:
    """Render one user event into provider-visible text."""
    if prefix := _user_prefix(sender, sent_at):
        content = f"[{prefix}]: {content}"
    if media:
        stubs = "\n".join(
            (
                f"[media: {path}] "
                "(call annotate_media with this exact path before your final response if it has not been annotated yet)"
            )
            for path in media
        )
        content = f"{content}\n{stubs}" if content else stubs
    return content


def _truncate_inline_images(content: object, max_chars: int | None) -> object:
    if max_chars is None:
        return content
    if isinstance(content, list):
        return [_truncate_inline_images(item, max_chars) for item in content]
    if isinstance(content, dict):
        if content.get("type") == "image_url":
            url = (content.get("image_url") or {}).get("url", "")
            truncated = url[:max_chars] + "…" if len(url) > max_chars else url
            return {"type": "image_url", "image_url": {"url": truncated}}
        return {key: _truncate_inline_images(value, max_chars) for key, value in content.items()}
    return content


@dataclass
class BaseEvent:
    timestamp: datetime = field(default_factory=now_aware)

    def __post_init__(self) -> None:
        self.timestamp = ensure_aware(self.timestamp)

    @property
    def kind(self) -> EventKind:
        raise NotImplementedError

    def _record_base(self) -> dict[str, Any]:
        return {
            "_type": "event",
            "kind": self.kind,
            "timestamp": self.timestamp.isoformat(timespec="seconds"),
        }

    def to_record(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_llm_message(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class UserEvent(BaseEvent):
    KIND: ClassVar[Literal["user"]] = "user"

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    media: list[str] = field(default_factory=list)
    media_metadata: list[MediaMetadata] = field(default_factory=list)
    sender_id: str | None = None
    sender_label: str | None = None

    class RenderKwargs(TypedDict, total=False):
        pending_image_blocks: list[dict[str, object]]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.sender_label is None:
            self.sender_label = _sender_label(self.metadata)

    @property
    def kind(self) -> Literal["user"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        record = {**self._record_base(), "content": self.content}
        optional_fields = {
            "metadata": self.metadata or None,
            "media": self.media or None,
            "media_metadata": self.media_metadata or None,
            "sender_id": self.sender_id,
            "sender_label": self.sender_label,
        }
        for key, value in optional_fields.items():
            if value is not None:
                record[key] = value
        return record

    def to_llm_message(self, **kwargs: Unpack[RenderKwargs]) -> dict[str, Any]:
        text = _render_user_content(
            self.content,
            media=self.media,
            sender=self.sender_label,
            sent_at=self.timestamp,
        )
        pending_image_blocks = kwargs.get("pending_image_blocks")
        content: str | list[dict[str, object]]
        if pending_image_blocks:
            content = [*pending_image_blocks, {"type": "text", "text": text}]
        else:
            content = text
        return {
            "role": "user",
            "content": content,
        }


@dataclass
class AssistantEvent(BaseEvent):
    KIND: ClassVar[Literal["assistant"]] = "assistant"

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None

    class RenderKwargs(TypedDict, total=False):
        include_reasoning: bool

    @property
    def kind(self) -> Literal["assistant"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        record = {**self._record_base(), "content": self.content}
        optional_fields = {
            "metadata": self.metadata or None,
            "tool_calls": self.tool_calls,
            "reasoning_content": self.reasoning_content,
        }
        for key, value in optional_fields.items():
            if value is not None:
                record[key] = value
        return record

    def to_llm_message(self, **kwargs: Unpack[RenderKwargs]) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        include_reasoning = kwargs.get("include_reasoning", True)
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if include_reasoning and self.reasoning_content:
            reasoning_content = self.reasoning_content
            if len(reasoning_content) > _MAX_REASONING_CHARS:
                reasoning_content = reasoning_content[:_MAX_REASONING_CHARS] + " [truncated]"
            message["reasoning_content"] = reasoning_content
        return message


@dataclass
class ToolEvent(BaseEvent):
    KIND: ClassVar[Literal["tool"]] = "tool"

    content: ToolResult = ""
    tool_call_id: str = ""
    tool_name: str = ""

    @property
    def kind(self) -> Literal["tool"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        return {
            **self._record_base(),
            "content": self.content,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
        }

    def to_llm_message(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.tool_name,
            "content": self.content,
        }


@dataclass
class SystemEvent(BaseEvent):
    KIND: ClassVar[Literal["system"]] = "system"

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # When True, the renderer hides this event once any UserEvent appears
    # later in the event list. The event still lives in session.jsonl for
    # replay fidelity. See spec/TOOL_REMINDERS.md.
    ephemeral: bool = False

    @property
    def kind(self) -> Literal["system"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        record = {**self._record_base(), "content": self.content}
        if self.metadata:
            record["metadata"] = self.metadata
        if self.ephemeral:
            record["ephemeral"] = True
        return record

    def to_llm_message(self, **kwargs: Any) -> dict[str, Any]:
        return {"role": "user", "content": f"<system_event>{self.content}</system_event>"}


@dataclass
class SummaryEvent(BaseEvent):
    KIND: ClassVar[Literal["summary"]] = "summary"

    content: str = ""

    @property
    def kind(self) -> Literal["summary"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        return {**self._record_base(), "content": self.content}

    def to_llm_message(self, **kwargs: Any) -> dict[str, Any]:
        return {"role": "user", "content": self.content}


ConversationEvent = UserEvent | AssistantEvent | ToolEvent | SystemEvent | SummaryEvent


def _clear_record(action: ClearAction) -> dict[str, Any]:
    """Build a persistence marker line written by :meth:`Session.clear`.

    On load, events written before the most recent clear marker are
    dropped from the in-memory history. The audit trail (the older
    events) stays on disk.
    """
    return {
        "_type": "marker",
        "kind": "clear",
        "action": action,
        "timestamp": now_aware().isoformat(timespec="seconds"),
    }


def event_from_record(record: dict[str, Any]) -> ConversationEvent:
    kind = record["kind"]
    timestamp = _parse_timestamp(record["timestamp"])
    if kind == "user":
        return UserEvent(
            timestamp=timestamp,
            content=str(record.get("content", "")),
            metadata=dict(record.get("metadata") or {}),
            media=list(record.get("media") or []),
            media_metadata=list(record.get("media_metadata") or []),
            sender_id=record.get("sender_id"),
            sender_label=record.get("sender_label"),
        )
    if kind == "assistant":
        return AssistantEvent(
            timestamp=timestamp,
            content=str(record.get("content", "")),
            metadata=dict(record.get("metadata") or {}),
            tool_calls=record.get("tool_calls"),
            reasoning_content=record.get("reasoning_content"),
        )
    if kind == "tool":
        return ToolEvent(
            timestamp=timestamp,
            content=record.get("content", ""),
            tool_call_id=str(record["tool_call_id"]),
            tool_name=str(record["tool_name"]),
        )
    if kind == "system":
        return SystemEvent(
            timestamp=timestamp,
            content=str(record.get("content", "")),
            metadata=dict(record.get("metadata") or {}),
            ephemeral=bool(record.get("ephemeral", False)),
        )
    if kind == "summary":
        return SummaryEvent(timestamp=timestamp, content=str(record.get("content", "")))
    raise ValueError(f"Unsupported event kind: {kind}")


@dataclass
class Session:
    """
    A conversation session.

    Stores typed conversation events in JSONL format for persistence.
    """

    addr: MessageAddress
    events: list[ConversationEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=now_aware)
    updated_at: datetime = field(default_factory=now_aware)
    metadata: dict[str, Any] = field(default_factory=dict)
    compacted_through: int = -1
    _log_path: Path | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.created_at = ensure_aware(self.created_at)
        self.updated_at = ensure_aware(self.updated_at)

    def attach_log(self, path: Path, *, write_header: bool) -> None:
        """Bind this session to a JSONL file for incremental writes.

        ``write_header=True`` (new session): create the file and write the
        metadata header. ``write_header=False`` (loaded session): reuse the
        existing file; subsequent ``append`` / ``clear`` calls write one
        line each, so a kill -9 mid-turn never drops the in-memory tail.
        """
        self._log_path = path
        if write_header:
            self.save(path)

    def _append_log_line(self, record: dict[str, Any]) -> None:
        if self._log_path is None:
            return
        with open(self._log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def append(self, event: ConversationEvent) -> None:
        self.events.append(event)
        self.updated_at = now_aware()
        self._append_log_line(event.to_record())

    def compact(self) -> None:
        self.append(
            SummaryEvent(
                content="[Context compacted to stay within context window limits.]"
            )
        )
        self.compacted_through = len(self.events) - 1

    @staticmethod
    def _render_event_message(
        event: ConversationEvent,
        *,
        options: RenderOptions,
        pending_media_blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        if isinstance(event, AssistantEvent):
            message = event.to_llm_message(include_reasoning=options.include_reasoning)
        elif isinstance(event, UserEvent):
            message = event.to_llm_message(pending_image_blocks=pending_media_blocks or [])
        else:
            message = event.to_llm_message()
        message["content"] = _truncate_inline_images(
            message.get("content", ""),
            options.max_inline_image_url_chars,
        )
        return message

    @staticmethod
    def _find_last_reasoning_index(history: list[ConversationEvent]) -> int | None:
        for i, event in reversed(list(enumerate(history))):
            if isinstance(event, AssistantEvent) and event.reasoning_content:
                return i
        return None

    def _build_pending_media_blocks(
        self,
        media_repo: MediaRenderer | None,
        options: RenderOptions,
    ) -> list[dict[str, object]] | None:
        if not options.pending_media_paths:
            return None
        if media_repo is None:
            return None
        return media_repo.build_media_blocks(options.pending_media_paths)

    def render_history(
        self,
        history: list[ConversationEvent],
        *,
        media_repo: MediaRenderer | None = None,
        options: RenderOptions | None = None,
    ) -> list[dict[str, object]]:
        options = options or RenderOptions()
        last_reasoning_idx = self._find_last_reasoning_index(history)
        last_user_idx = max(
            (i for i, ev in enumerate(history) if isinstance(ev, UserEvent)),
            default=-1,
        )
        pending_media_blocks = self._build_pending_media_blocks(media_repo, options)
        messages: list[dict[str, object]] = []
        for i, event in enumerate(history):
            if (
                isinstance(event, SystemEvent)
                and event.ephemeral
                and i < last_user_idx
            ):
                continue
            messages.append(
                self._render_event_message(
                    event,
                    options=RenderOptions(
                        include_reasoning=options.include_reasoning and i == last_reasoning_idx,
                        pending_media_paths=None,
                        max_inline_image_url_chars=options.max_inline_image_url_chars,
                    ),
                    pending_media_blocks=pending_media_blocks if i == len(history) - 1 else None,
                )
            )
        return messages

    def render_llm_messages(
        self,
        system_prompt: str,
        media_repo: MediaRenderer | None,
        options: RenderOptions | None = None,
    ) -> list[dict[str, object]]:
        history = self.get_history_events()
        return [
            {"role": "system", "content": system_prompt},
            *self.render_history(history, media_repo=media_repo, options=options),
        ]

    def get_history_events(self) -> list[ConversationEvent]:
        """Return the current typed conversation history.

        After a compaction, the ``SummaryEvent`` at ``compacted_through``
        replaces everything before it; otherwise all events are returned
        verbatim. Bounding history length is the compactor's job — this
        method does not trim, so the rendered prefix stays cache-stable
        between compactions.
        """
        if self.compacted_through >= 0 and self.events:
            return [
                self.events[self.compacted_through],
                *self.events[self.compacted_through + 1 :],
            ]
        return list(self.events)

    def clear(self, action: ClearAction = "reset") -> None:
        """Clear all events from the in-memory history.

        Writes a clear marker to the log file so the prior conversation is
        retained on disk; events before the most recent marker are dropped
        from the rendered history at load time.
        """
        self.events = []
        self.compacted_through = -1
        self.updated_at = now_aware()
        self._append_log_line(_clear_record(action))

    def describe_current_session(self) -> str:
        """Return a readable prompt label for the current chat when possible."""
        channel_name = _channel_display_name(self.addr.channel)
        last_user = next(
            (event for event in reversed(self.events) if isinstance(event, UserEvent)),
            None,
        )
        if not last_user:
            return f"{channel_name} chat {self.addr.chat_id}"

        sender = str(last_user.sender_label or "").strip() or None
        is_group = bool(last_user.metadata.get("is_group"))

        if sender and not is_group:
            return f"{sender} on {channel_name}"
        if sender and is_group:
            return f"{channel_name} group chat (recent sender: {sender})"
        return f"{channel_name} chat {self.addr.chat_id}"

    @classmethod
    def load(cls, path: Path) -> "Session | None":
        """Load a session from a JSONL file. Returns None if file missing or invalid."""
        if not path.exists():
            return None

        try:
            events: list[ConversationEvent] = []
            metadata = {}
            created_at = None
            updated_at = None
            compacted_through = -1
            addr: MessageAddress | None = None

            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)
                    record_type = data.get("_type")

                    if record_type == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = (
                            _parse_timestamp(data["created_at"]) if data.get("created_at") else None
                        )
                        updated_at = (
                            _parse_timestamp(data["updated_at"]) if data.get("updated_at") else None
                        )
                        compacted_through = data.get("compacted_through", -1)
                        if data.get("address"):
                            addr = MessageAddress.from_string(data["address"])
                    elif record_type == "marker" and data.get("kind") == "clear":
                        # Drop everything before the most recent clear; the
                        # audit trail stays on disk but isn't rendered.
                        events = []
                        compacted_through = -1
                    else:
                        events.append(event_from_record(data))

            if addr is None:
                logger.warning(f"No address in session file {path}, skipping")
                return None

            return cls(
                addr=addr,
                events=events,
                created_at=created_at or now_aware(),
                updated_at=updated_at or now_aware(),
                metadata=metadata,
                compacted_through=compacted_through,
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
                "compacted_through": self.compacted_through,
            }
            f.write(json.dumps(metadata_line) + "\n")
            for event in self.events:
                f.write(json.dumps(event.to_record()) + "\n")


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    Use as an async context manager: all sessions are loaded on enter and flushed on exit.
    """

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self._archive_dir = sessions_dir / ".archive"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[MessageAddress, Session] = {}

    def _get_session_path(self, key: MessageAddress) -> Path:
        return self.sessions_dir / f"{sanitize_filename(str(key).replace(':', ''))}.jsonl"

    async def __aenter__(self) -> "SessionManager":
        sessions: list[Session] = []
        for path in self.sessions_dir.glob("*.jsonl"):
            if (session := Session.load(path)) is not None:
                sessions.append(session)

        if len(sessions) > MAX_SESSIONS:
            sessions.sort(key=lambda s: s.updated_at, reverse=True)
            for old_session in sessions[MAX_SESSIONS:]:
                self._archive(old_session)
            sessions = sessions[:MAX_SESSIONS]

        for session in sessions:
            session.attach_log(self._get_session_path(session.addr), write_header=False)
        self._cache = {s.addr: s for s in sessions}
        return self

    async def __aexit__(self, *_: Any) -> None:
        # No-op: every Session.append already wrote its line. Kept so
        # ``async with`` callers don't have to change.
        return None

    def _archive(self, s: Session) -> None:
        path = self._get_session_path(s.addr)
        archive_path = (
            self._archive_dir / f"{path.stem}_{now_aware().strftime('%Y%m%dT%H%M%S')}{path.suffix}"
        )

        self._archive_dir.mkdir(parents=True, exist_ok=True)
        s.save(archive_path)
        path.unlink(missing_ok=True)

    def save(self, session: Session) -> None:
        session.save(self._get_session_path(session.addr))

    def get(self, key: MessageAddress) -> Session:
        if key not in self._cache:
            session = Session(addr=key)
            session.attach_log(self._get_session_path(key), write_header=True)
            self._cache[key] = session
            if len(self._cache) > MAX_SESSIONS:
                oldest = min(
                    (s for s in self._cache.values() if s.addr != key),
                    key=lambda s: s.updated_at,
                )
                self._archive(oldest)
                del self._cache[oldest.addr]
        return self._cache[key]

    def clear(self, key: MessageAddress) -> None:
        if s := self._cache.pop(key, None):
            self._archive(s)
