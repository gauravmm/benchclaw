"""Utility functions for nanobot."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonlines


def read_jsonl(path: Path) -> list[Any]:
    """Read all valid entries from a JSONL file; returns [] if file doesn't exist."""
    if not path.exists():
        return []
    with jsonlines.open(path) as reader:
        return list(reader.iter(skip_invalid=True))


def write_jsonl(path: Path, entries: list[dict]) -> None:
    """Overwrite a JSONL file with entries."""
    with jsonlines.open(path, mode="w") as writer:
        writer.write_all(entries)


def append_jsonl(path: Path, entries: list[dict]) -> None:
    """Append entries to a JSONL file."""
    with jsonlines.open(path, mode="a") as writer:
        writer.write_all(entries)


def _ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_workspace_path() -> Path:
    "Get the workspace path."
    return _ensure_dir(Path("./workspace"))


def get_media_path() -> Path:
    return _ensure_dir(get_workspace_path() / "media")


def _sanitize_path_segment(segment: str) -> str:
    """Sanitize a path segment so it is safe across platforms."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", segment).strip("._")
    return cleaned or "unknown"


def get_timestamped_media_dir(
    channel: str,
    chat_id: str,
    timestamp: datetime | None = None,
    workspace: Path | None = None,
) -> Path:
    """Return media/{channel}/{chat_id}/{YYYYMMDD_HHMMSS}/ within the workspace."""
    ws = workspace or get_workspace_path()
    ts = (timestamp or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return _ensure_dir(
        ws / "media" / _sanitize_path_segment(channel) / _sanitize_path_segment(chat_id) / ts
    )


# TODO: Figure out where to move this.
def get_skills_path(workspace: Path | None = None) -> Path:
    """Get the skills directory within the workspace."""
    ws = workspace or get_workspace_path()
    return _ensure_dir(ws / "skills")


def truncate_string(s: str, max_len: int = 100, suffix: str = "...") -> str:
    """Truncate a string to max length, adding suffix if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix
