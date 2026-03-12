"""WhatsApp channel package."""

from typing import Any

from benchclaw.channels.whatsapp.address import (
    normalize_whatsapp_address,
    normalize_whatsapp_chat_id,
    normalize_whatsapp_person_id,
    outbound_whatsapp_chat_id,
    parse_normalized_whatsapp_address,
    whatsapp_addresses_match,
)

__all__ = [
    "WhatsAppChannel",
    "WhatsAppConfig",
    "normalize_whatsapp_address",
    "normalize_whatsapp_chat_id",
    "normalize_whatsapp_person_id",
    "outbound_whatsapp_chat_id",
    "parse_normalized_whatsapp_address",
    "whatsapp_addresses_match",
]


def __getattr__(name: str) -> Any:
    if name in {"WhatsAppChannel", "WhatsAppConfig"}:
        from benchclaw.channels.whatsapp.channel import WhatsAppChannel, WhatsAppConfig

        return {"WhatsAppChannel": WhatsAppChannel, "WhatsAppConfig": WhatsAppConfig}[name]
    raise AttributeError(name)
