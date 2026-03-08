"""Utility functions for nanobot."""

from datetime import datetime
from pathlib import Path


def _ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_workspace_path() -> Path:
    "Get the workspace path."
    return _ensure_dir(Path("./workspace"))


def get_media_path() -> Path:
    return _ensure_dir(get_workspace_path() / "media")


def get_timestamped_media_path(workspace: Path | None = None) -> Path:
    """Create and return a timestamped subdirectory under the media folder.

    Each call creates a unique folder named YYYYMMDD_HHMMSS, suitable for
    grouping all files received in a single message together.
    """
    base = (workspace / "media") if workspace else get_media_path()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return _ensure_dir(base / ts)


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
