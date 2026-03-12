"""WhatsApp channel package."""

from typing import Any

from benchclaw.channels.whatsapp.address import (
    WhatsAppId,
)

__all__ = [
    "WhatsAppChannel",
    "WhatsAppConfig",
    "WhatsAppId",
]


def __getattr__(name: str) -> Any:
    if name in {"WhatsAppChannel", "WhatsAppConfig"}:
        from benchclaw.channels.whatsapp.channel import WhatsAppChannel, WhatsAppConfig

        return {"WhatsAppChannel": WhatsAppChannel, "WhatsAppConfig": WhatsAppConfig}[name]
    raise AttributeError(name)
