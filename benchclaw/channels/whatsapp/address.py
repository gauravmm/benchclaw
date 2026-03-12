"""Canonical WhatsApp address normalization shared across tools and channels."""

from __future__ import annotations

from dataclasses import dataclass

from benchclaw.bus import MessageAddress


def _split_jid(value: str) -> tuple[str, str | None]:
    local, sep, domain = value.strip().lower().partition("@")
    local = local.split(":", 1)[0]
    return local, domain if sep else None


@dataclass(frozen=True, slots=True)
class WhatsAppId:
    """Canonical WhatsApp identity used throughout the app."""

    canonical: str

    @classmethod
    def from_raw(cls, value: str) -> "WhatsAppId":
        local, domain = _split_jid(value)
        if not local:
            return cls("")
        if domain == "g.us":
            return cls(f"{local}@{domain}")
        return cls(local)

    @classmethod
    def from_address(cls, address: MessageAddress) -> "WhatsAppId":
        if address.channel != "whatsapp":
            raise ValueError(f"Expected whatsapp address, got {address.channel!r}")
        return cls.from_raw(address.chat_id)

    @property
    def is_group(self) -> bool:
        return self.canonical.endswith("@g.us")

    @property
    def localpart(self) -> str:
        return self.canonical.split("@", 1)[0]

    def as_address(self) -> MessageAddress:
        return MessageAddress("whatsapp", self.canonical)

    def outbound_jid(self) -> str:
        if not self.canonical or self.is_group:
            return self.canonical
        return f"{self.canonical}@s.whatsapp.net"

    def __str__(self) -> str:
        return self.canonical
