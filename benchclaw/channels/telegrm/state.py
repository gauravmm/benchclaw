"""Typed inbound/outbound state objects for the Telegram channel.

The outbound pipeline (``outbound.py``) plans an :class:`OutboundMessage`
into a list of :class:`OutboundSegment`s, then dispatches each segment.
Adding a new content shape means adding a dataclass here and a match
arm in ``dispatch``; nothing else moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TextSegment:
    body: str


@dataclass(frozen=True)
class MediaSegment:
    path: Path
    mime: str
    caption: str | None


OutboundSegment = TextSegment | MediaSegment
