"""Typed outbound state for the WhatsApp channel.

Mirrors :mod:`benchclaw.channels.telegrm.state` so the per-segment
dispatch in ``outbound.py`` reads the same way for both channels.

Phase B grows :class:`MediaSegment` MIME-aware dispatch on the bridge
side; for now ``MediaSegment(mime=...)`` is the typed shape that the
plan/dispatch pair operates on.
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
