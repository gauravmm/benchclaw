"""Tests for MediaRepository."""

import json
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

    path = repo.register(
        _telegram("123456"),
        sender_id="123456",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )

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

    p1 = repo.register(
        _telegram("123456"),
        sender_id="123456",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )
    p2 = repo.register(
        _telegram("123456"),
        sender_id="123456",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )

    assert p1.name == "1423-01.jpg"
    assert p2.name == "1423-02.jpg"


def test_register_different_senders_different_dirs(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    p1 = repo.register(
        _telegram("alice"),
        sender_id="alice",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )
    p2 = repo.register(
        _telegram("bob"),
        sender_id="bob",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )

    assert p1.parent.parent != p2.parent.parent  # different hash dirs


def test_media_relpath(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    path = repo.register(
        _wa("555"),
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )
    rel = repo.media_relpath(path)

    assert rel.startswith("media/")
    assert rel.endswith(".jpg")


def test_set_caption(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    path = repo.register(
        _wa("555"),
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )
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

    p1 = repo.register(
        _wa("555"),
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )
    p1.touch()

    # Create a new repo instance and reload — serial should continue from 1
    repo2 = MediaRepository(media_dir)
    repo2.load()
    p2 = repo2.register(
        _wa("555"),
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
    )

    assert p1.name == "1423-01.jpg"
    assert p2.name == "1423-02.jpg"


def test_purge_old(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media", max_age_days=30)
    repo.load()

    old_ts = datetime(2025, 1, 1, 12, 0, 0)
    new_ts = datetime(2026, 3, 10, 14, 23, 0)

    old_path = repo.register(
        _wa("555"),
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=old_ts,
    )
    new_path = repo.register(
        _wa("555"),
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=new_ts,
    )
    old_path.touch()
    new_path.touch()

    deleted = repo.purge_old()

    assert deleted == 1
    assert not old_path.exists()
    assert new_path.exists()


def test_register_persists_address_and_metadata(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)
    address = MessageAddress("telegram", "chat-42")

    path = repo.register(
        address,
        sender_id="alice",
        media_type="image",
        ext=".png",
        mime_type="image/png",
        timestamp=ts,
        original_name="receipt.png",
    )

    repo2 = MediaRepository(tmp_path / "media")
    repo2.load()
    rel = repo.media_relpath(path)
    record = next(item for item in repo2.iter_records() if item["path"] == rel)

    assert record["address"] == "telegram:chat-42"
    assert record["sender_id"] == "alice"
    assert record["media_type"] == "image"
    assert record["original_name"] == "receipt.png"


def test_load_legacy_entry_without_address(tmp_path: Path):
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True)
    legacy = {
        "hash/0310/1423/01": {
            "sender_id": "legacy-user",
            "timestamp": "2026-03-10T14:23:00",
            "mime_type": "image/jpeg",
            "ext": ".jpg",
            "caption": "legacy image",
        }
    }
    (media_dir / ".meta.json").write_text(json.dumps(legacy), encoding="utf-8")

    repo = MediaRepository(media_dir)
    repo.load()
    [record] = list(repo.iter_records())

    assert record["address"] is None
    assert record["sender_id"] == "legacy-user"
    assert record["caption"] == "legacy image"


def test_search_scopes_by_address_and_caption(tmp_path: Path):
    repo = MediaRepository(tmp_path / "media")
    repo.load()
    ts = datetime(2026, 3, 10, 14, 23, 0)

    first = repo.register(
        MessageAddress("telegram", "chat-a"),
        sender_id="alice",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts,
        original_name="receipt.jpg",
    )
    second = repo.register(
        MessageAddress("telegram", "chat-b"),
        sender_id="bob",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=ts.replace(minute=24),
        original_name="cat.jpg",
    )
    repo.set_caption(repo.media_relpath(first), "grocery receipt with apples")
    repo.set_caption(repo.media_relpath(second), "cat on sofa")

    results = repo.search(
        query="receipt",
        address=MessageAddress("telegram", "chat-a"),
        limit=10,
    )

    assert [item["path"] for item in results] == [repo.media_relpath(first)]


def test_search_includes_legacy_entries_in_global_search(tmp_path: Path):
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True)
    meta = {
        "hash/0310/1423/01": {
            "sender_id": "legacy-user",
            "timestamp": "2026-03-10T14:23:00",
            "mime_type": "image/jpeg",
            "ext": ".jpg",
            "caption": "legacy cat photo",
        }
    }
    (media_dir / ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

    repo = MediaRepository(media_dir)
    repo.load()
    results = repo.search(query="legacy cat", limit=5)

    assert len(results) == 1
    assert results[0]["address"] is None
