"""Pretty-printed prompt dump for debugging.

Isolated from loop logic so tweaks to the dump format don't churn the
agent loop. Marked omitted from coverage in ``pyproject.toml``.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger


def dump_messages(path: Path | None, messages: list[dict[str, object]]) -> None:
    if path is None:
        return
    try:
        path.write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to write debug dump: {e}")
