"""Structured media repository for incoming channel media."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from benchclaw.bus import MessageAddress
from benchclaw.channels.whatsapp.address import (
    normalize_whatsapp_address,
    parse_normalized_whatsapp_address,
    whatsapp_addresses_match,
)


class MediaEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    address: MessageAddress | None = None
    sender_id: str
    timestamp: str  # ISO
    media_type: str = "file"
    mime_type: str | None
    ext: str  # file extension including dot, e.g. ".jpg"
    original_name: str | None = None
    caption: str | None = None

    @field_validator("address", mode="before")
    @classmethod
    def _parse_address(cls, value: Any) -> MessageAddress | None:
        if value is None or isinstance(value, MessageAddress):
            return normalize_whatsapp_address(value) if isinstance(value, MessageAddress) else value
        if isinstance(value, str):
            return parse_normalized_whatsapp_address(value)
        raise TypeError(f"Unsupported media address value: {value!r}")

    @field_serializer("address")
    def _serialize_address(self, value: MessageAddress | None) -> str | None:
        return str(value) if value else None


class MediaRepository:
    """
    Persistent store for media files received from channels.

    On-disk format: <media_dir>/<hash8>/<mmdd>/<hhmm>-<serial_2d>.<ext>  (unchanged)
    Metadata key format in .meta.json: <hash8>/<mmdd>/<hhmm>/<serial>
    Workspace-relative paths (e.g. "media/a3f7b2c1/0310/1423-01.jpg") are
    used throughout so the LLM can reference them in <image_caption> tags.
    """

    def __init__(self, media_dir: Path, max_age_days: int = 30) -> None:
        self.media_dir = media_dir
        self.max_age_days = max_age_days
        # "hash8/mmdd/hhmm" -> {"01" -> entry}
        # Serial = max key in each bucket + 1; no separate counter needed.
        self._entries: dict[str, dict[str, MediaEntry]] = {}

    def load(self) -> None:
        """Load metadata from .meta.json.

        Invariant: every registered file has a .meta.json entry, so the metadata
        is the sole source of truth — no disk scan needed.
        """
        meta_path = self.media_dir / ".meta.json"
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                for key, entry_dict in data.items():
                    bucket, serial = key.rsplit("/", 1)
                    self._entries.setdefault(bucket, {})[serial] = MediaEntry.model_validate(
                        entry_dict
                    )
            except Exception as e:
                logger.warning(f"Failed to load media metadata: {e}")

    def save(self) -> None:
        """Write metadata to .meta.json."""
        self.media_dir.mkdir(parents=True, exist_ok=True)
        meta_path = self.media_dir / ".meta.json"
        data = {
            f"{bucket}/{serial}": entry.model_dump(mode="json")
            for bucket, serials in self._entries.items()
            for serial, entry in serials.items()
        }
        meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def register(
        self,
        address: MessageAddress,
        sender_id: str,
        media_type: str,
        ext: str,
        mime_type: str | None,
        timestamp: datetime | None = None,
        original_name: str | None = None,
    ) -> Path:
        """
        Allocate a new media file path, record the entry, save metadata, and return the abs path.
        Caller must write the file bytes to the returned path.
        """
        assert ext.startswith(".") or ext == "", "ext should include the dot, e.g. '.jpg'"
        ts = timestamp or datetime.now()
        hhmm = ts.strftime("%H%M")
        mmdd = ts.strftime("%m%d")
        bucket = f"{address.hash8}/{mmdd}/{hhmm}"
        serial = f"{max(map(int, self._entries.get(bucket, {})), default=0) + 1:02d}"
        abs_path = self.media_dir / address.hash8 / mmdd / f"{hhmm}-{serial}{ext}"
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        self._entries.setdefault(bucket, {})[serial] = MediaEntry(
            address=address,
            sender_id=sender_id,
            timestamp=ts.isoformat(timespec="seconds"),
            media_type=media_type,
            mime_type=mime_type,
            ext=ext,
            original_name=original_name,
            caption=None,
        )
        self.save()
        return abs_path

    def media_relpath(self, abs_path: Path) -> str:
        """Return workspace-relative path: 'media/<hash8>/<mmdd>/<hhmm>-<serial>.<ext>'"""
        workspace = self.media_dir.parent
        return str(abs_path.relative_to(workspace))

    def set_caption(self, path: str, caption: str) -> None:
        """Update the caption for a media entry. path is workspace-relative (e.g. 'media/...')."""
        try:
            media, rest = path.split("/", 1)
            assert media == self.media_dir.name
            hash8_mmdd, filename = rest.rsplit("/", 1)
            stem = filename.rsplit(".", 1)[0]  # "hhmm-serial"
            hhmm, serial = stem.split("-", 1)
            self._entries[f"{hash8_mmdd}/{hhmm}"][serial].caption = caption
        except ValueError, AssertionError, KeyError:
            raise KeyError(f"set_caption: no entry for {path}")
        self.save()

    def iter_records(self) -> Iterable[dict[str, Any]]:
        """Yield stored media records with their workspace-relative path."""
        for bucket, serials in self._entries.items():
            hash8, mmdd, hhmm = bucket.split("/")
            for serial, entry in serials.items():
                relpath = f"{self.media_dir.name}/{hash8}/{mmdd}/{hhmm}-{serial}{entry.ext}"
                yield {
                    "path": relpath,
                    "address": str(entry.address) if entry.address else None,
                    "sender_id": entry.sender_id,
                    "timestamp": entry.timestamp,
                    "media_type": entry.media_type,
                    "mime_type": entry.mime_type,
                    "original_name": entry.original_name,
                    "caption": entry.caption,
                }

    def search(
        self,
        *,
        query: str | None = None,
        address: MessageAddress | None = None,
        sender_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search media metadata deterministically using stored fields and captions."""
        limit = max(1, min(limit, 20))
        needle = (query or "").strip().casefold()
        lower_from = self._parse_date_bound(date_from, end=False)
        upper_to = self._parse_date_bound(date_to, end=True)
        matches: list[tuple[int, datetime, dict[str, Any]]] = []

        for record in self.iter_records():
            record_ts = datetime.fromisoformat(record["timestamp"])
            record_addr = record["address"]
            if address is not None and not whatsapp_addresses_match(record_addr, str(address)):
                continue
            if sender_id is not None and record["sender_id"] != sender_id:
                continue
            if lower_from is not None and record_ts < lower_from:
                continue
            if upper_to is not None and record_ts > upper_to:
                continue

            score = self._score_record(record, needle)
            if needle and score < 0:
                continue
            matches.append((score, record_ts, record))

        matches.sort(key=lambda item: (-item[0], -item[1].timestamp(), item[2]["path"]))
        return [record for _, _, record in matches[:limit]]

    @staticmethod
    def _parse_date_bound(value: str | None, *, end: bool) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        if "T" in value:
            return parsed
        if end:
            return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _score_record(record: dict[str, Any], needle: str) -> int:
        if not needle:
            return 0
        path = str(record["path"]).casefold()
        name = (record.get("original_name") or "").casefold()
        caption = (record.get("caption") or "").casefold()
        mime = (record.get("mime_type") or "").casefold()
        address = (record.get("address") or "").casefold()
        sender_id = (record.get("sender_id") or "").casefold()

        if any(value == needle for value in (path, name, address, sender_id)):
            return 300
        if needle in path or needle in name:
            return 200
        if needle in caption:
            return 100
        if any(needle in hay for hay in (mime, address, sender_id)):
            return 50
        return -1

    async def __aenter__(self) -> "MediaRepository":
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.load()
        if purged := self._purge_old():
            logger.info(f"Purged {purged} old media files")
        return self

    async def __aexit__(self, *_: object) -> None:
        pass  # save() is called after every mutation

    def _purge_old(self) -> int:
        """Delete files older than max_age_days. Removes empty dirs. Returns count deleted."""
        today = datetime.now().date()
        today_md = today.strftime("%m%d")
        cutoff_md = (today - timedelta(days=self.max_age_days)).strftime("%m%d")
        deleted = 0
        for bucket in list(self._entries):
            hash8, mmdd, hhmm = bucket.split("/")
            # Delete if mmdd is outside the rolling [cutoff_md, today_md] window.
            # XOR of the two boundary comparisons, flipped by the year-wrap flag, handles both cases.
            if (mmdd < cutoff_md) ^ (mmdd > today_md) ^ (cutoff_md > today_md):
                deleted += len(self._entries[bucket])
                for serial, entry in self._entries.pop(bucket).items():
                    (self.media_dir / hash8 / mmdd / f"{hhmm}-{serial}{entry.ext}").unlink(
                        missing_ok=True
                    )

        # Remove empty directories (deepest first)
        for d in sorted(self.media_dir.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()

        if deleted:
            self.save()
        return deleted
