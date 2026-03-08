"""Tests for MessageAddress and Session serialization round-trips."""

from datetime import datetime
from pathlib import Path

import pytest

from benchclaw.bus import MessageAddress
from benchclaw.session import MAX_SESSIONS, Session, SessionManager

# ---------------------------------------------------------------------------
# MessageAddress
# ---------------------------------------------------------------------------


def test_message_address_str():
    addr = MessageAddress(channel="telegram", chat_id="123")
    assert str(addr) == "telegram:123"


def test_message_address_from_string():
    addr = MessageAddress.from_string("telegram:123")
    assert addr.channel == "telegram"
    assert addr.chat_id == "123"


def test_message_address_from_string_roundtrip():
    addr = MessageAddress(channel="whatsapp", chat_id="456@s.whatsapp.net")
    assert MessageAddress.from_string(str(addr)) == addr


def test_message_address_from_string_colon_in_chat_id():
    """chat_id may itself contain colons; only the first colon is the delimiter."""
    addr = MessageAddress.from_string("telegram:123:456")
    assert addr.channel == "telegram"
    assert addr.chat_id == "123:456"


# ---------------------------------------------------------------------------
# Session.save / Session.load
# ---------------------------------------------------------------------------


def test_session_save_load_roundtrip(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="99")
    session = Session(addr=addr)
    session.add_message(
        "user",
        "hello",
        media=["workspace/media/telegram/99/20260308_101530/abc.jpg"],
        media_metadata=[
            {
                "path": "workspace/media/telegram/99/20260308_101530/abc.jpg",
                "media_type": "image",
                "mime_type": "image/jpeg",
                "size_bytes": 12345,
                "saved_at": "2026-03-08T10:15:30",
                "source_channel": "telegram",
                "original_name": None,
            }
        ],
    )
    session.add_message("assistant", "hi there", tools_used=["search"])

    path = tmp_path / "session.jsonl"
    session.save(path)

    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.addr == addr
    assert len(loaded.messages) == 2
    assert loaded.messages[0]["content"] == "hello"
    assert loaded.messages[0]["media"] == ["workspace/media/telegram/99/20260308_101530/abc.jpg"]
    assert loaded.messages[0]["media_metadata"][0]["media_type"] == "image"
    assert loaded.messages[1]["tools_used"] == ["search"]


def test_session_load_missing_file(tmp_path: Path):
    assert Session.load(tmp_path / "nonexistent.jsonl") is None


def test_session_load_missing_address(tmp_path: Path):
    """A JSONL file without an address field should return None."""
    path = tmp_path / "bad.jsonl"
    path.write_text('{"_type": "metadata", "created_at": "2024-01-01T00:00:00"}\n')
    assert Session.load(path) is None


def test_session_load_preserves_timestamps(tmp_path: Path):
    addr = MessageAddress(channel="smtp", chat_id="user@example.com")
    created = datetime(2024, 6, 1, 12, 0, 0)
    session = Session(addr=addr, created_at=created)

    path = tmp_path / "session.jsonl"
    session.save(path)

    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.created_at == created


def test_session_load_preserves_metadata(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="42")
    session = Session(addr=addr, metadata={"thread": "xyz"})

    path = tmp_path / "session.jsonl"
    session.save(path)

    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.metadata == {"thread": "xyz"}


def test_session_clear(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="1")
    session = Session(addr=addr)
    session.add_message("user", "test")
    session.clear()
    assert session.messages == []
    assert session.last_consolidated == 0


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_manager_get_or_create(tmp_path: Path):
    async with SessionManager(tmp_path) as sm:
        addr = MessageAddress(channel="telegram", chat_id="1")
        s1 = sm.get(addr)
        s2 = sm.get(addr)
        assert s1 is s2


@pytest.mark.asyncio
async def test_session_manager_persists_on_exit(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="1")

    async with SessionManager(tmp_path) as sm:
        s = sm.get(addr)
        s.add_message("user", "persisted")

    # Re-enter and check the session was saved
    async with SessionManager(tmp_path) as sm2:
        s2 = sm2.get(addr)
        assert len(s2.messages) == 1
        assert s2.messages[0]["content"] == "persisted"


@pytest.mark.asyncio
async def test_session_manager_save_midway(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="2")

    async with SessionManager(tmp_path) as sm:
        s = sm.get(addr)
        s.add_message("user", "mid")
        sm.save(s)

    path = tmp_path / "telegram2.jsonl"
    assert path.exists()
    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "mid"


@pytest.mark.asyncio
async def test_session_manager_clear_archives(tmp_path: Path):
    addr = MessageAddress(channel="telegram", chat_id="3")

    async with SessionManager(tmp_path) as sm:
        s = sm.get(addr)
        s.add_message("user", "to be archived")
        sm.save(s)
        sm.clear(addr)

    archive_dir = tmp_path / ".archive"
    archived = list(archive_dir.glob("*.jsonl"))
    assert len(archived) == 1


@pytest.mark.asyncio
async def test_session_manager_max_sessions(tmp_path: Path):
    """Sessions beyond MAX_SESSIONS are archived on __aenter__."""
    # Pre-create MAX_SESSIONS + 5 session files
    for i in range(MAX_SESSIONS + 5):
        addr = MessageAddress(channel="telegram", chat_id=str(i))
        s = Session(addr=addr, updated_at=datetime(2024, 1, 1, hour=i % 24))
        path = tmp_path / f"telegram{i}.jsonl"
        s.save(path)

    async with SessionManager(tmp_path) as sm:
        assert len(sm._cache) == MAX_SESSIONS

    archive_dir = tmp_path / ".archive"
    archived = list(archive_dir.glob("*.jsonl"))
    assert len(archived) == 5
