"""Telegram channel configuration + slash-command lists."""

from __future__ import annotations

from typing import TYPE_CHECKING

from benchclaw.bus import MessageBus
from benchclaw.channels.base import ChannelConfig
from benchclaw.media import MediaRepository

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


class TelegramConfig(ChannelConfig):
    """Telegram channel configuration."""

    token: str = ""  # Bot token from @BotFather
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )

    def make_channel(
        self, bus: MessageBus, media_repo: MediaRepository | None = None
    ) -> "TelegramChannel":
        from benchclaw.channels.telegrm.channel import TelegramChannel

        return TelegramChannel(self, bus, media_repo=media_repo)

    def is_configured(self) -> bool:
        return bool(self.token.strip())
