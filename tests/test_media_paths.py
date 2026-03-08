"""Tests for timestamped media directory helpers."""

from datetime import datetime
from pathlib import Path

from benchclaw.utils import get_timestamped_media_dir


def test_get_timestamped_media_dir_shape(tmp_path: Path):
    ts = datetime(2026, 3, 8, 14, 5, 6)
    path = get_timestamped_media_dir(
        channel="telegram",
        chat_id="123456",
        timestamp=ts,
        workspace=tmp_path,
    )

    expected = tmp_path / "media" / "telegram" / "123456" / "20260308_140506"
    assert path == expected
    assert path.exists()
    assert path.is_dir()


def test_get_timestamped_media_dir_sanitizes_segments(tmp_path: Path):
    ts = datetime(2026, 3, 8, 14, 5, 6)
    path = get_timestamped_media_dir(
        channel="tele gram",
        chat_id="group:foo/bar",
        timestamp=ts,
        workspace=tmp_path,
    )

    expected = tmp_path / "media" / "tele_gram" / "group_foo_bar" / "20260308_140506"
    assert path == expected
