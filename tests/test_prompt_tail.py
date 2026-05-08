"""Tests for the tail-injection mechanism in :mod:`benchclaw.agent.prompt`.

The tail keeps turn-local context (current time, future workspace
listing, etc.) in a synthetic user message right before the latest user
turn rather than inside the system prompt. Two invariants matter:

1. The system prompt stays byte-identical across turns (no time stamp).
2. ``stable_prefix_end`` points at the synthetic tail message, so the
   cache monitor sees the prefix that should hit upstream caches.
"""

from __future__ import annotations

import pytest

from benchclaw.agent.prompt import (
    PromptBuild,
    _insert_synthetic_tail,
    build_system_prompt,
)
from benchclaw.bus import MessageAddress


def _addr() -> MessageAddress:
    return MessageAddress(channel="telegram", chat_id="1")


def test_system_prompt_is_byte_stable_across_calls(tmp_path) -> None:
    """No turn-local fields (Time / live state) leak into the system
    prompt — calling build twice in a row gives identical bytes."""
    a = build_system_prompt(tmp_path, channel="telegram", chat_id="1")
    b = build_system_prompt(tmp_path, channel="telegram", chat_id="1")
    assert a == b
    assert "Time:" not in a


def test_insert_tail_places_synthetic_message_before_latest_user() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
    ]
    out, stable_prefix_end = _insert_synthetic_tail(messages, [("current_time", "now")])

    # Synthetic message lives at index 3 (where the second user used to be).
    assert out[3]["role"] == "user"
    assert out[3]["content"] == "<current_time>now</current_time>"
    # Original "second" user moves to index 4.
    assert out[4]["content"] == "second"
    # stable_prefix_end is the synthetic message index, which equals the
    # original last_user_idx — everything before it is cacheable.
    assert stable_prefix_end == 3


def test_insert_tail_with_no_blocks_returns_messages_unchanged() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hi"},
    ]
    out, stable_prefix_end = _insert_synthetic_tail(messages, [])
    assert out == messages
    assert stable_prefix_end == 1


def test_insert_tail_with_no_user_messages_returns_messages_unchanged() -> None:
    messages: list[dict[str, object]] = [{"role": "system", "content": "S"}]
    out, stable_prefix_end = _insert_synthetic_tail(messages, [("current_time", "now")])
    assert out == messages
    assert stable_prefix_end == len(messages)


def test_insert_tail_renders_multiple_blocks_in_order() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hello"},
    ]
    out, _ = _insert_synthetic_tail(
        messages,
        [("current_time", "T1"), ("workspace_listing", "a.txt\nb.txt")],
    )
    assert out[1]["role"] == "user"
    assert (
        out[1]["content"]
        == "<current_time>T1</current_time>\n<workspace_listing>a.txt\nb.txt</workspace_listing>"
    )


def test_promptbuild_dataclass_is_frozen() -> None:
    """PromptBuild is intentionally immutable — the loop passes it to the
    cache monitor and the LLM call without expecting anyone to mutate it."""
    pb = PromptBuild(messages=[{"role": "user", "content": "x"}], stable_prefix_end=0)
    with pytest.raises(Exception):
        pb.stable_prefix_end = 1  # type: ignore[misc]
