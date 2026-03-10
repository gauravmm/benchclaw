"""Base channel interface for chat platforms."""

import asyncio
from abc import abstractmethod
from asyncio import Task
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Self

from anyio import AsyncContextManagerMixin
from loguru import logger
from pydantic import BaseModel
from pydantic import field_serializer, field_validator

from benchclaw.bus import InboundMessage, MediaMetadata, MessageAddress, MessageBus, OutboundMessage

_CONFIG_REGISTRY: dict[str, type["ChannelConfig"]] = {}


def register_channel(name: str, cls: type["ChannelConfig"]) -> None:
    """Register a channel config class under the given name."""
    _CONFIG_REGISTRY[name] = cls


class ChannelConfig(BaseModel):
    allow_from: list[str] | None = None
    summon: str = "always"
    summon_lookback: timedelta = timedelta(minutes=5)
    summon_max_gap: timedelta = timedelta(minutes=2)

    @field_validator("summon", mode="before")
    @classmethod
    def _validate_summon(cls, value: Any) -> str:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
            aliases = {
                "none": "always",
                "off": "always",
                "disabled": "always",
                "mention_or_reply": "mention_or_reply",
                "mention|reply": "mention_or_reply",
                "mention,reply": "mention_or_reply",
            }
            normalized = aliases.get(normalized, normalized)
            if normalized in {"always", "mention", "reply", "mention_or_reply"}:
                return normalized
        raise ValueError("summon must be one of: always, mention, reply, mention_or_reply")

    @field_validator("summon_lookback", "summon_max_gap", mode="before")
    @classmethod
    def _parse_duration(cls, value: Any) -> timedelta:
        if isinstance(value, timedelta):
            return value
        if isinstance(value, (int, float)):
            return timedelta(seconds=float(value))
        if isinstance(value, str):
            text = value.strip().lower()
            if not text:
                raise ValueError("duration string cannot be empty")
            parts = text.split()
            total = timedelta()
            if len(parts) > 1:
                if len(parts) % 2 != 0:
                    raise ValueError(f"invalid duration format: {value}")
                for i in range(0, len(parts), 2):
                    total += cls._duration_part(parts[i], parts[i + 1])
                return total
            import re

            matches = re.findall(r"([0-9]*\.?[0-9]+)\s*([a-z]+)", text)
            if not matches:
                raise ValueError(f"invalid duration format: {value}")
            compact = "".join(f"{amount}{unit}" for amount, unit in matches)
            if compact != text.replace(" ", ""):
                raise ValueError(f"invalid duration format: {value}")
            for amount, unit in matches:
                total += cls._duration_part(amount, unit)
            return total
        raise ValueError("duration must be a timedelta, number of seconds, or duration string")

    @classmethod
    def _duration_part(cls, amount: str, unit: str) -> timedelta:
        value = float(amount)
        unit_map = {
            "s": "seconds",
            "sec": "seconds",
            "secs": "seconds",
            "second": "seconds",
            "seconds": "seconds",
            "m": "minutes",
            "min": "minutes",
            "mins": "minutes",
            "minute": "minutes",
            "minutes": "minutes",
            "h": "hours",
            "hr": "hours",
            "hrs": "hours",
            "hour": "hours",
            "hours": "hours",
        }
        normalized = unit_map.get(unit)
        if not normalized:
            raise ValueError(f"invalid duration unit: {unit}")
        return timedelta(**{normalized: value})

    @field_serializer("summon_lookback", "summon_max_gap")
    def _serialize_duration(self, value: timedelta) -> str:
        total = int(value.total_seconds())
        if total % 3600 == 0:
            return f"{total // 3600}h"
        if total % 60 == 0:
            return f"{total // 60}m"
        return f"{total}s"

    def make_channel(self, bus: "MessageBus") -> "BaseChannel":
        raise NotImplementedError(f"{type(self).__name__} must implement make_channel()")


class BaseChannel(AsyncContextManagerMixin):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the nanobot message bus.
    """

    name: str = "base"

    def __init__(self, config: ChannelConfig, bus: MessageBus):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.bus = bus

        self._task: Task | None = None  # Background task
        self._inbound_filter = InboundAttentionFilter(config)

    async def background(self) -> None:
        """
        Start the channel and begin listening for messages.

        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        4. Terminates cleanly on CancelledError
        """

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self.background())
        return self

    async def __aexit__(self, *args):
        if self._task:
            try:
                async with asyncio.timeout(5):
                    self._task.cancel()
            except TimeoutError:
                print(f"{self.__qualname__} timed out while exiting")

    def status(self) -> tuple[bool, str]:
        """Return (is_running, description) for this channel."""
        running = bool(self._task and not self._task.done())
        return (running, "running" if running else "stopped")

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.

        Args:
            msg: The message to send.
        """
        pass

    def is_allowed(self, sender_id: str) -> bool:
        """
        Check if a sender is allowed to use this bot.

        Args:
            sender_id: The sender's identifier.

        Returns:
            True if allowed, False otherwise.
        """
        allow_list = self.config.allow_from
        if not allow_list:
            # If no allow list, allow everyone
            return True

        if sender_id in allow_list:
            return True

        if "|" in sender_id:
            if any(part in allow_list for part in sender_id.split("|")):
                return True
        return False

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        media_metadata: list[MediaMetadata] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions and forwards to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            media_metadata: Optional structured metadata for media attachments.
            metadata: Optional channel-specific metadata.
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                f"Access denied for sender {sender_id} on channel {self.name}. "
                f"Add them to allow_from list in config to grant access."
            )
            return

        message_timestamp = occurred_at or datetime.now()
        outgoing = self._inbound_filter.filter(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            media_metadata=media_metadata or [],
            metadata=metadata or {},
            timestamp=message_timestamp,
        )
        for item in outgoing:
            msg = InboundMessage(
                address=MessageAddress(channel=self.name, chat_id=str(chat_id)),
                sender_id=item.sender_id,
                content=item.content,
                timestamp=item.timestamp,
                media=item.media,
                media_metadata=item.media_metadata,
                metadata=item.metadata,
            )
            await self.bus.publish_inbound(msg)


class SummonMode(str, Enum):
    ALWAYS = "always"
    MENTION = "mention"
    REPLY = "reply"
    MENTION_OR_REPLY = "mention_or_reply"


@dataclass
class _BufferedInbound:
    sender_id: str
    content: str
    media: list[str]
    media_metadata: list[MediaMetadata]
    metadata: dict[str, Any]
    timestamp: datetime
    forwarded: bool = False


@dataclass
class _AttentionState:
    active: bool = False
    source: str | None = None
    last_activity: datetime | None = None
    backlog: deque[_BufferedInbound] | None = None


class InboundAttentionFilter:
    """Reusable inbound filter that gates group messages behind summon triggers."""

    def __init__(self, config: ChannelConfig):
        self._config = config
        self._mode = SummonMode(config.summon)
        self._states: dict[tuple[str, str], _AttentionState] = {}

    def filter(
        self,
        *,
        channel: str,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str],
        media_metadata: list[MediaMetadata],
        metadata: dict[str, Any],
        timestamp: datetime,
    ) -> list[_BufferedInbound]:
        is_group = bool(metadata.get("is_group", False))
        payload = _BufferedInbound(
            sender_id=sender_id,
            content=content,
            media=media,
            media_metadata=media_metadata,
            metadata=dict(metadata),
            timestamp=timestamp,
        )
        if not is_group or self._mode == SummonMode.ALWAYS:
            return [payload]

        key = (channel, chat_id)
        state = self._states.setdefault(key, _AttentionState(backlog=deque()))
        assert state.backlog is not None
        state.backlog.append(payload)
        self._trim_backlog(state.backlog, timestamp)

        summon_source = self._extract_summon_source(payload.metadata)

        if state.active and state.last_activity:
            if timestamp - state.last_activity > self._config.summon_max_gap:
                state.active = False
                state.source = None

        if state.active:
            state.last_activity = timestamp
            if summon_source:
                state.source = summon_source
            source = state.source or summon_source
            if source:
                payload.metadata["summon"] = source
            payload.forwarded = True
            return [payload]

        if not summon_source:
            return []

        state.active = True
        state.source = summon_source
        state.last_activity = timestamp

        selected = self._select_context(state.backlog, timestamp)
        for item in selected:
            item.forwarded = True
            item.metadata["summon"] = summon_source
        return selected

    def _extract_summon_source(self, metadata: dict[str, Any]) -> str | None:
        raw = metadata.pop("_summon_source", None)
        if not isinstance(raw, str):
            return None
        source = raw.strip().lower().replace("-", "_")
        allowed = {
            SummonMode.MENTION: {"mention"},
            SummonMode.REPLY: {"reply"},
            SummonMode.MENTION_OR_REPLY: {"mention", "reply"},
            SummonMode.ALWAYS: {"mention", "reply"},
        }
        return source if source in allowed[self._mode] else None

    def _trim_backlog(self, backlog: deque[_BufferedInbound], now: datetime) -> None:
        cutoff = now - self._config.summon_lookback
        while backlog and backlog[0].timestamp < cutoff:
            backlog.popleft()

    def _select_context(
        self, backlog: deque[_BufferedInbound], now: datetime
    ) -> list[_BufferedInbound]:
        selected: list[_BufferedInbound] = []
        previous: _BufferedInbound | None = None
        for item in reversed(backlog):
            if item.forwarded:
                break
            if now - item.timestamp > self._config.summon_lookback:
                break
            if previous and previous.timestamp - item.timestamp > self._config.summon_max_gap:
                break
            selected.append(item)
            previous = item
        selected.reverse()
        return selected
