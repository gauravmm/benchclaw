"""WhatsApp channel configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from benchclaw.bus import MessageBus
from benchclaw.channels.base import ChannelConfig
from benchclaw.media import MediaRepository

if TYPE_CHECKING:
    from benchclaw.channels.whatsapp.channel import WhatsAppChannel


class WhatsAppConfig(ChannelConfig):
    """WhatsApp channel configuration."""

    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (optional, recommended)

    def make_channel(
        self, bus: MessageBus, media_repo: MediaRepository | None = None
    ) -> "WhatsAppChannel":
        from benchclaw.channels.whatsapp.channel import WhatsAppChannel

        return WhatsAppChannel(self, bus, media_repo=media_repo)

    def is_configured(self) -> bool:
        return bool(self.bridge_url.strip())
