"""Explicit built-in channel manifest."""

from benchclaw.channels.base import ChannelConfig
from benchclaw.channels.telegrm import TelegramConfig
from benchclaw.channels.whatsapp.channel import WhatsAppConfig

BUILTIN_CHANNEL_CONFIGS: tuple[tuple[str, type[ChannelConfig]], ...] = (
    ("telegram", TelegramConfig),
    ("whatsapp", WhatsAppConfig),
)
