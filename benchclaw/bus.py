"""Async message bus for decoupled channel-agent communication."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MessageAddress:
    """Identifies a conversation endpoint (channel + chat_id)."""

    channel: str
    chat_id: str

    def __str__(self) -> str:
        return f"{self.channel}:{self.chat_id}"

    @classmethod
    def from_string(cls, s: str) -> "MessageAddress":
        channel, chat_id = s.split(":", 1)
        return cls(channel=channel, chat_id=chat_id)


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    address: MessageAddress  # Channel + chat_id endpoint
    sender_id: str  # User identifier (the specific person within the group chat)
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data

    # TODO: Remove these and use .address directly.
    @property
    def channel(self) -> str:
        return self.address.channel

    @property
    def chat_id(self) -> str:
        return self.address.chat_id


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    address: MessageAddress  # Destination endpoint
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def channel(self) -> str:
        return self.address.channel

    @property
    def chat_id(self) -> str:
        return self.address.chat_id


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Outbound messages are queued per-channel; any consumer for that channel
    receives the next message regardless of which chat it came from.

    Usage:
        await bus.publish_outbound(msg)          # enqueues to msg.channel queue
        await bus.consume_outbound(channel="x")  # next message for channel x
    """

    # FUTURE: Support a channel bias (that is, if messages are recieved on multiple channels, prioritize the channel currently being worked on.)

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: dict[str, asyncio.Queue[OutboundMessage]] = {}
        self._channel_created = asyncio.Condition()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Enqueue a response into the channel's outbound queue, creating it if needed."""
        if msg.channel not in self.outbound:
            # NOTE: The check (msg.channel not in self.outbound) is NOT redundant.
            # There's a race condition where two threads fail the check above, then one clobbers the other's
            # queue assignment. The fix is to protect the check with the lock. To avoid the slowdown of acquiring
            # locks on every publish (when only the first call will fail the check), we guard the lock itself
            # with the check above.
            async with self._channel_created:
                if msg.channel not in self.outbound:
                    self.outbound.setdefault(msg.channel, asyncio.Queue())
                    self._channel_created.notify_all()
        await self.outbound[msg.channel].put(msg)

    async def consume_outbound(self, *, channel: str) -> OutboundMessage:
        """Block until the next outbound message for the given channel is available.

        If the channel queue does not exist yet, waits until it is created by
        the first publish_outbound call for that channel.
        """
        if channel not in self.outbound:
            async with self._channel_created:
                await self._channel_created.wait_for(lambda: channel in self.outbound)
        return await self.outbound[channel].get()
