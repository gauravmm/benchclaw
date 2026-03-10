"""Structured media repository for incoming channel media."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from benchclaw.bus import MessageAddress


class MediaEntry(BaseModel):
    channel: str
    sender_id: str
    timestamp: str  # ISO
    mime_type: str | None
    ext: str  # file extension including dot, e.g. ".jpg"
    caption: str | None = None


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
            f"{bucket}/{serial}": entry.model_dump()
            for bucket, serials in self._entries.items()
            for serial, entry in serials.items()
        }
        meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def register(
        self,
        sender: MessageAddress,
        ext: str,
        mime_type: str | None,
        timestamp: datetime | None = None,
    ) -> Path:
        """
        Allocate a new media file path, record the entry, save metadata, and return the abs path.
        Caller must write the file bytes to the returned path.
        """
        assert ext.startswith(".") or ext == "", "ext should include the dot, e.g. '.jpg'"
        ts = timestamp or datetime.now()
        hhmm = ts.strftime("%H%M")
        mmdd = ts.strftime("%m%d")
        bucket = f"{sender.hash8}/{mmdd}/{hhmm}"
        serial = f"{max(map(int, self._entries.get(bucket, {})), default=-1) + 1:02d}"
        abs_path = self.media_dir / sender.hash8 / mmdd / f"{hhmm}-{serial}{ext}"
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        self._entries.setdefault(bucket, {})[serial] = MediaEntry(
            channel=sender.channel,
            sender_id=sender.chat_id,
            timestamp=ts.isoformat(timespec="seconds"),
            mime_type=mime_type,
            ext=ext,
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

    async def __aenter__(self) -> "MediaRepository":
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.load()
        if purged := self.purge_old():
            logger.info(f"Purged {purged} old media files")
        return self

    async def __aexit__(self, *_: object) -> None:
        pass  # save() is called after every mutation

    def purge_old(self) -> int:
        """Delete files older than max_age_days. Removes empty dirs. Returns count deleted."""
        cutoff = datetime.now() - timedelta(days=self.max_age_days)
        deleted = 0
        for bucket in list(self._entries):
            hash8_mmdd, hhmm = bucket.rsplit("/", 1)
            for serial, entry in list(self._entries[bucket].items()):
                try:
                    ts = datetime.fromisoformat(entry.timestamp)
                except ValueError, TypeError:
                    continue
                if ts < cutoff:
                    (self.media_dir / hash8_mmdd / f"{hhmm}-{serial}{entry.ext}").unlink(
                        missing_ok=True
                    )
                    del self._entries[bucket][serial]
                    deleted += 1
            if not self._entries[bucket]:
                del self._entries[bucket]

        # Remove empty bucket directories (deepest first)
        for d in sorted(self.media_dir.glob("*/*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        for d in sorted(self.media_dir.glob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()

        if deleted:
            self.save()
        return deleted
