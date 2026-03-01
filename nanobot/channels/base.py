"""Base channel interface for chat platforms."""

import asyncio
from abc import abstractmethod
from asyncio import Task
from typing import Any, Self

from anyio import AsyncContextManagerMixin
from loguru import logger
from pydantic import BaseModel

from nanobot.bus import InboundMessage, MessageAddress, MessageBus, OutboundMessage

_CONFIG_REGISTRY: dict[str, type["ChannelConfig"]] = {}


def register_channel(name: str, cls: type["ChannelConfig"]) -> None:
    """Register a channel config class under the given name."""
    _CONFIG_REGISTRY[name] = cls


class ChannelConfig(BaseModel):
    allow_from: list[str] | None = None

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
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions and forwards to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            metadata: Optional channel-specific metadata.
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                f"Access denied for sender {sender_id} on channel {self.name}. "
                f"Add them to allow_from list in config to grant access."
            )
            return

        msg = InboundMessage(
            address=MessageAddress(channel=self.name, chat_id=str(chat_id)),
            sender_id=str(sender_id),
            content=content,
            media=media or [],
            metadata=metadata or {},
        )

        await self.bus.publish_inbound(msg)
