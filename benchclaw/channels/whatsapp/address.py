"""Canonical WhatsApp address normalization shared across tools and channels."""

from __future__ import annotations

from benchclaw.bus import MessageAddress


def _split_jid(value: str) -> tuple[str, str | None]:
    text = value.strip().lower()
    local, sep, domain = text.partition("@")
    local = local.split(":", 1)[0]
    return local, domain if sep else None


def normalize_whatsapp_chat_id(chat_id: str) -> str:
    """Return the canonical internal WhatsApp chat ID."""
    local, domain = _split_jid(chat_id)
    if not local:
        return ""
    if domain == "g.us":
        return f"{local}@{domain}"
    return local


def normalize_whatsapp_person_id(person_id: str) -> str:
    """Normalize a person JID for identity comparisons and name cache lookups."""
    local, domain = _split_jid(person_id)
    if not local:
        return ""
    if domain == "g.us":
        return f"{local}@{domain}"
    return local


def normalize_whatsapp_address(address: MessageAddress) -> MessageAddress:
    """Normalize a MessageAddress if it targets WhatsApp."""
    if address.channel != "whatsapp":
        return address
    return MessageAddress(address.channel, normalize_whatsapp_chat_id(address.chat_id))


def parse_normalized_whatsapp_address(value: str | None) -> MessageAddress | None:
    """Parse ``channel:chat_id`` and normalize WhatsApp addresses."""
    if not value:
        return None
    return normalize_whatsapp_address(MessageAddress.from_string(value))


def whatsapp_addresses_match(left: str | None, right: str | None) -> bool:
    """Compare stored and requested WhatsApp addresses under canonical rules."""
    if left is None or right is None:
        return left == right
    left_addr = parse_normalized_whatsapp_address(left)
    right_addr = parse_normalized_whatsapp_address(right)
    if left_addr and right_addr and left_addr.channel == right_addr.channel == "whatsapp":
        return left_addr == right_addr
    return left == right


def outbound_whatsapp_chat_id(chat_id: str) -> str:
    """Convert canonical internal WhatsApp chat IDs to routable bridge targets."""
    canonical = normalize_whatsapp_chat_id(chat_id)
    if not canonical or "@" in canonical:
        return canonical
    return f"{canonical}@s.whatsapp.net"
