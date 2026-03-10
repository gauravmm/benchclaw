"""Utility functions for nanobot."""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, TypeAlias

import jsonlines
from pydantic import BeforeValidator, PlainSerializer
from pytimeparse.timeparse import timeparse


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


def parse_duration(value: timedelta | int | float | str, positive: bool = True) -> timedelta:
    """Parse duration from timedelta, numeric seconds, or pytimeparse-compatible text."""
    assert not isinstance(value, bool), "Duration must not be a boolean."
    if isinstance(value, timedelta):
        result = value

    elif isinstance(value, int | float):
        assert math.isfinite(value), "Duration must be finite."
        result = timedelta(seconds=float(value))

    elif isinstance(value, str):
        parsed_seconds = timeparse(value)
        assert parsed_seconds is not None, "pytimeparse parsing failed."
        result = timedelta(seconds=parsed_seconds)

    assert not positive or result > timedelta(0), "Duration must be greater than zero."
    return result


DurationField: TypeAlias = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(str),
]
