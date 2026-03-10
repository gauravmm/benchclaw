"""Tests for MediaRepository."""

from datetime import datetime
from pathlib import Path

from benchclaw.bus import MessageAddress
from benchclaw.media import MediaRepository


def _telegram(sender_id: str) -> MessageAddress:
    return MessageAddress("telegram", sender_id)


def _wa(sender_id: str) -> MessageAddress:
    return MessageAddress("wa", sender_id)


def test_register_path_shape(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    path = repo.register(_telegram("123456"), ".jpg", "image/jpeg", ts)

    # Path format: <media_dir>/<hash8>/0310/1423-01.jpg
    assert path.parent.parent.parent == repo.media_dir
    assert path.parent.name == "0310"
    assert path.name.endswith("-01.jpg")
    assert path.name.startswith("1423-")
    assert path.parent.parent.parent.parent == tmp_path


def test_register_serial_increments(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    p1 = repo.register(_telegram("123456"), ".jpg", "image/jpeg", ts)
    p2 = repo.register(_telegram("123456"), ".jpg", "image/jpeg", ts)

    assert p1.name == "1423-01.jpg"
    assert p2.name == "1423-02.jpg"


def test_register_different_senders_different_dirs(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    p1 = repo.register(_telegram("alice"), ".jpg", "image/jpeg", ts)
    p2 = repo.register(_telegram("bob"), ".jpg", "image/jpeg", ts)

    assert p1.parent.parent != p2.parent.parent  # different hash dirs


def test_media_relpath(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    path = repo.register(_wa("555"), ".jpg", "image/jpeg", ts)
    rel = repo.media_relpath(path)

    assert rel.startswith("media/")
    assert rel.endswith(".jpg")


def test_set_caption(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    path = repo.register(_wa("555"), ".jpg", "image/jpeg", ts)
    rel = repo.media_relpath(path)  # e.g. "media/abc/0310/1423-01.jpg"

    repo.set_caption(rel, "a dog sitting on grass")

    media_rel = rel[len("media/") :]  # strip "media/" prefix e.g. "hash8/mmdd/hhmm-01.jpg"
    hash8_mmdd, filename = media_rel.rsplit("/", 1)
    hhmm, serial = filename.rsplit(".", 1)[0].split("-", 1)
    assert repo._entries[f"{hash8_mmdd}/{hhmm}"][serial].caption == "a dog sitting on grass"


def test_serial_rebuilt_after_reload(tmp_path: Path):
    media_dir = tmp_path / "media"
    repo = MediaRepository(media_dir)
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    p1 = repo.register(_wa("555"), ".jpg", "image/jpeg", ts)
    p1.touch()

    # Create a new repo instance and reload — serial should continue from 1
    repo2 = MediaRepository(media_dir)
    repo2.load()
    p2 = repo2.register(_wa("555"), ".jpg", "image/jpeg", ts)

    assert p1.name == "1423-01.jpg"
    assert p2.name == "1423-02.jpg"


def test_purge_old(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media", max_age_days=30)
    repo.load()

    old_ts = datetime(2025, 1, 1, 12, 0, 0)
    new_ts = datetime(2026, 3, 10, 14, 23, 0)

    old_path = repo.register(_wa("555"), ".jpg", "image/jpeg", old_ts)
    new_path = repo.register(_wa("555"), ".jpg", "image/jpeg", new_ts)
    old_path.touch()
    new_path.touch()

    deleted = repo.purge_old()

    assert deleted == 1
    assert not old_path.exists()
    assert new_path.exists()
