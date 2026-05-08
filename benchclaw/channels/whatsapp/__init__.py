"""WhatsApp channel package."""

from benchclaw.channels.whatsapp.address import WhatsAppId
from benchclaw.channels.whatsapp.channel import WhatsAppChannel
from benchclaw.channels.whatsapp.config import WhatsAppConfig

__all__ = [
    "WhatsAppChannel",
    "WhatsAppConfig",
    "WhatsAppId",
]
