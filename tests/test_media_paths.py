"""Tests for MediaRepository."""

import json
from datetime import datetime
from pathlib import Path

from benchclaw.bus import MessageAddress
from benchclaw.media import MediaRepository

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _telegram(sender_id: str) -> MessageAddress:
    return MessageAddress("telegram", sender_id)


def _wa(sender_id: str) -> MessageAddress:
    return MessageAddress("wa", sender_id)


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_1X1)


def test_register_path_shape(tmp_path: Path):
    repo = MediaRepository(tmp_path)
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

    assert path.parent.parent.parent == repo.media_dir
    assert path.parent.name == "0310"
    assert path.name == "1423-01.jpg"


def test_register_serial_increments(tmp_path: Path):
    repo = MediaRepository(tmp_path)
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
    repo = MediaRepository(tmp_path)
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

    assert p1.parent.parent != p2.parent.parent


def test_media_relpath(tmp_path: Path):
    repo = MediaRepository(tmp_path)
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

    assert repo.media_relpath(path).startswith("media/")


def test_set_caption_updates_registered_record(tmp_path: Path):
    repo = MediaRepository(tmp_path)
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
    path.write_bytes(b"jpeg-bytes")

    repo.set_caption(rel, "a dog sitting on grass")

    assert repo._entries[rel].caption == "a dog sitting on grass"


def test_set_caption_creates_generic_workspace_record(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    repo.load()
    path = tmp_path / "images" / "receipt.png"
    _write_png(path)

    repo.set_caption("images/receipt.png", "store receipt")

    entry = repo._entries["images/receipt.png"]
    assert entry.caption == "store receipt"
    assert entry.original_name == "receipt.png"
    assert entry.mime_type == "image/png"
    assert entry.media_type == "image"


def test_resolve_file_returns_absolute_path_and_mime_type(tmp_path: Path):
    repo = MediaRepository(tmp_path)
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
    path.write_bytes(b"jpeg-bytes")

    resolved_path, mime_type = repo.resolve_file(repo.media_relpath(path))

    assert resolved_path == path
    assert mime_type == "image/jpeg"


def test_resolve_file_uses_detected_mime_type_when_metadata_is_missing(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    repo.load()
    path = tmp_path / "images" / "pixel.png"
    _write_png(path)

    resolved_path, mime_type = repo.resolve_file("images/pixel.png")

    assert resolved_path == path
    assert mime_type == "image/png"


def test_serial_rebuilt_after_reload(tmp_path: Path):
    repo = MediaRepository(tmp_path)
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

    repo2 = MediaRepository(tmp_path)
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


def test_purge_old_only_removes_old_registered_media(tmp_path: Path):
    repo = MediaRepository(tmp_path, max_age_days=30)
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
    notes = tmp_path / "notes" / "receipt.png"
    _write_png(notes)
    repo.set_caption("notes/receipt.png", "manual note image")
    old_path.touch()
    new_path.touch()

    deleted = repo._purge_old()

    assert deleted == 1
    assert not old_path.exists()
    assert new_path.exists()
    assert "notes/receipt.png" in repo._entries


def test_purge_old_handles_offset_aware_timestamps(tmp_path: Path):
    repo = MediaRepository(tmp_path, max_age_days=30)
    repo.load()

    path = repo.register(
        _wa("555"),
        sender_id="555",
        media_type="image",
        ext=".jpg",
        mime_type="image/jpeg",
        timestamp=datetime(2025, 1, 1, 12, 0, 0),
    )
    path.touch()
    rel = repo.media_relpath(path)
    repo._entries[rel].timestamp = (
        datetime(2025, 1, 1, 12, 0, 0).astimezone().isoformat(timespec="seconds")
    )

    deleted = repo._purge_old()

    assert deleted == 1
    assert not path.exists()


def test_register_persists_address_and_metadata(tmp_path: Path):
    repo = MediaRepository(tmp_path)
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

    repo2 = MediaRepository(tmp_path)
    repo2.load()
    rel = repo.media_relpath(path)
    record = next(item for item in repo2.iter_records() if item["path"] == rel)

    assert record["address"] == "telegram:chat-42"
    assert record["sender_id"] == "alice"
    assert record["media_type"] == "image"
    assert record["original_name"] == "receipt.png"


def test_save_uses_nested_path_dictionary_at_workspace_root(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    repo.load()
    path = tmp_path / "images" / "receipt.png"
    _write_png(path)

    repo.set_caption("images/receipt.png", "receipt image")

    meta = json.loads((tmp_path / ".media.json").read_text(encoding="utf-8"))
    assert meta["images"]["receipt.png"]["_entry"]["caption"] == "receipt image"


def test_load_nested_path_keyed_entry_without_address(tmp_path: Path):
    nested = {
        "images": {
            "receipt.png": {
                "_entry": {
                    "sender_id": "legacy-user",
                    "timestamp": "2026-03-10T14:23:00",
                    "mime_type": "image/jpeg",
                    "caption": "stored image",
                }
            }
        }
    }
    (tmp_path / ".media.json").write_text(json.dumps(nested), encoding="utf-8")

    repo = MediaRepository(tmp_path)
    repo.load()
    [record] = list(repo.iter_records())

    assert record["path"] == "images/receipt.png"
    assert record["address"] is None
    assert record["sender_id"] == "legacy-user"
    assert record["caption"] == "stored image"


def test_search_scopes_by_address_and_caption(tmp_path: Path):
    repo = MediaRepository(tmp_path)
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
    first.write_bytes(b"jpeg-bytes")
    second.write_bytes(b"jpeg-bytes")
    repo.set_caption(repo.media_relpath(first), "grocery receipt with apples")
    repo.set_caption(repo.media_relpath(second), "cat on sofa")

    results = repo.search(query="receipt", address=MessageAddress("telegram", "chat-a"), limit=10)

    assert [item["path"] for item in results] == [repo.media_relpath(first)]


def test_search_includes_generic_captioned_files(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    repo.load()
    path = tmp_path / "images" / "cat.png"
    _write_png(path)
    repo.set_caption("images/cat.png", "cat photo")

    results = repo.search(query="cat photo", limit=5)

    assert len(results) == 1
    assert results[0]["path"] == "images/cat.png"
    assert results[0]["address"] is None


def test_search_normalizes_whatsapp_address_matching(tmp_path: Path):
    nested = {
        "images": {
            "receipt.png": {
                "_entry": {
                    "address": "whatsapp:222355137806442@lid",
                    "sender_id": "whatsapp-user",
                    "timestamp": "2026-03-10T14:23:00",
                    "media_type": "image",
                    "mime_type": "image/jpeg",
                    "caption": "receipt photo",
                }
            }
        }
    }
    (tmp_path / ".media.json").write_text(json.dumps(nested), encoding="utf-8")

    repo = MediaRepository(tmp_path)
    repo.load()
    results = repo.search(
        query="receipt",
        address=MessageAddress("whatsapp", "222355137806442"),
        limit=5,
    )

    assert len(results) == 1
    assert results[0]["address"] == "whatsapp:222355137806442"
