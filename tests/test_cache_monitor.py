"""Tests for the prompt-cache divergence watchdog.

The monitor compares the stable prefix of consecutive prompt builds and
warns once per fingerprint when the cacheable prefix drifts. The warn
behaviour is verified via the Loguru sink rather than logger internals.
"""

from __future__ import annotations

from typing import Any

import pytest
from loguru import logger

from benchclaw.agent.cache_monitor import PromptCacheMonitor
from benchclaw.agent.prompt import PromptBuild
from benchclaw.bus import MessageAddress


@pytest.fixture
def captured_warnings():
    sink: list[str] = []
    handler_id = logger.add(lambda msg: sink.append(str(msg)), level="WARNING")
    try:
        yield sink
    finally:
        logger.remove(handler_id)


def _build(messages: list[dict[str, Any]], stable_prefix_end: int) -> PromptBuild:
    return PromptBuild(messages=messages, stable_prefix_end=stable_prefix_end)


def _addr() -> MessageAddress:
    return MessageAddress(channel="telegram", chat_id="1")


def test_observe_first_call_does_not_warn(captured_warnings: list[str]) -> None:
    monitor = PromptCacheMonitor()
    build = _build(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}],
        stable_prefix_end=1,
    )
    monitor.observe(_addr(), build)
    assert all("Prompt cache" not in line for line in captured_warnings)


def test_observe_warns_on_system_prompt_drift(captured_warnings: list[str]) -> None:
    monitor = PromptCacheMonitor()
    addr = _addr()

    monitor.observe(
        addr,
        _build(
            [{"role": "system", "content": "system A"}, {"role": "user", "content": "hi"}],
            stable_prefix_end=1,
        ),
    )
    monitor.observe(
        addr,
        _build(
            [{"role": "system", "content": "system B"}, {"role": "user", "content": "hi"}],
            stable_prefix_end=1,
        ),
    )
    assert any("system message diverged" in line for line in captured_warnings)


def test_observe_warns_only_once_per_fingerprint(captured_warnings: list[str]) -> None:
    monitor = PromptCacheMonitor()
    addr = _addr()

    monitor.observe(
        addr,
        _build(
            [{"role": "system", "content": "A"}, {"role": "user", "content": "x"}],
            stable_prefix_end=1,
        ),
    )
    # Same drift twice — should warn the first time and stay quiet the second.
    for _ in range(3):
        monitor.observe(
            addr,
            _build(
                [{"role": "system", "content": "B"}, {"role": "user", "content": "x"}],
                stable_prefix_end=1,
            ),
        )
    diverged = [line for line in captured_warnings if "system message diverged" in line]
    assert len(diverged) == 1


def test_observe_warns_on_history_drift(captured_warnings: list[str]) -> None:
    monitor = PromptCacheMonitor()
    addr = _addr()

    monitor.observe(
        addr,
        _build(
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
            ],
            stable_prefix_end=3,
        ),
    )
    # Second turn rewrites a historical assistant message — that should bust
    # the cache and surface a "history diverged" warning.
    monitor.observe(
        addr,
        _build(
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "rewritten answer"},
                {"role": "user", "content": "second question"},
            ],
            stable_prefix_end=3,
        ),
    )
    assert any("history diverged" in line for line in captured_warnings)


def test_forget_drops_state(captured_warnings: list[str]) -> None:
    """After ``forget``, the next observe should be treated as a fresh start
    (no warning), even if the system prompt would have differed from a
    previous snapshot."""
    monitor = PromptCacheMonitor()
    addr = _addr()

    monitor.observe(
        addr,
        _build(
            [{"role": "system", "content": "A"}, {"role": "user", "content": "x"}],
            stable_prefix_end=1,
        ),
    )
    monitor.forget(addr)
    monitor.observe(
        addr,
        _build(
            [{"role": "system", "content": "B"}, {"role": "user", "content": "x"}],
            stable_prefix_end=1,
        ),
    )
    assert all("Prompt cache" not in line for line in captured_warnings)
