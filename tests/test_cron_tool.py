"""Tests for the CronTool surface — schedule resolution, action dispatch,
and the tool-level error contracts that the agent loop depends on.

The CronStore state machine and JSONL round-trip are covered by
``test_cron_typesupport.py`` / ``test_cron_serialization.py``; this file
exercises the tool-level orchestration: how ``execute(action=...)``
maps to schedule construction, and where each error condition surfaces.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.cron.tool import CronTool, _parse_when
from benchclaw.agent.tools.cron.typesupport import (
    CronJob,
    CronScheduleAt,
    CronScheduleEvery,
    CronStore,
)
from benchclaw.bus import MessageAddress, MessageBus
from benchclaw.utils import now_aware


def _addr() -> MessageAddress:
    return MessageAddress(channel="telegram", chat_id="42")


async def _running_tool(tmp_path) -> tuple[CronTool, CronStore, MessageBus]:
    """Build a tool + an open CronStore. Caller closes the store via
    ``await store.__aexit__(None, None, None)``."""
    bus = MessageBus()
    bus.inbound[_addr()] = asyncio.Queue()
    tool = CronTool(store_path=tmp_path / "jobs.json", bus=bus)
    store = await CronStore(tool._store_path).__aenter__()
    tool._store = store
    return tool, store, bus


# ---------------------------------------------------------------------------
# _parse_when
# ---------------------------------------------------------------------------


def test_parse_when_iso_timestamp_returns_exact_datetime() -> None:
    """ISO inputs must round-trip through ``_parse_when`` to the same
    instant. The wall-clock fields will reflect the local timezone after
    ``_parse_timestamp`` normalisation, so compare on the underlying
    UTC instant."""
    from datetime import datetime, timezone

    parsed = _parse_when("2030-06-15T12:30:00+00:00")
    expected = datetime(2030, 6, 15, 12, 30, tzinfo=timezone.utc)
    assert parsed == expected


def test_parse_when_duration_string_returns_relative_to_now() -> None:
    before = now_aware()
    parsed = _parse_when("1h30m")
    after = now_aware()
    delta = parsed - before
    assert timedelta(hours=1, minutes=29) <= delta <= (after - before) + timedelta(hours=1, minutes=31)


def test_parse_when_invalid_iso_propagates_duration_failure() -> None:
    """When neither path parses, the duration parser raises and we let it
    bubble — _parse_when's job is to dispatch, not to swallow errors."""
    with pytest.raises(Exception):
        _parse_when("2030-99-99T99:99:99")


# ---------------------------------------------------------------------------
# CronTool._resolve_schedule
# ---------------------------------------------------------------------------


def test_resolve_schedule_delay_only_returns_one_shot() -> None:
    schedule = CronTool._resolve_schedule(delay="30m", every=None, until=None)
    assert isinstance(schedule, CronScheduleAt)


def test_resolve_schedule_every_only_anchors_one_period_from_now() -> None:
    before = now_aware()
    schedule = CronTool._resolve_schedule(delay=None, every="1h", until=None)
    assert isinstance(schedule, CronScheduleEvery)
    assert schedule.every == timedelta(hours=1)
    delta = schedule.anchor - before
    assert timedelta(minutes=59) <= delta <= timedelta(minutes=61)


def test_resolve_schedule_delay_and_every_uses_delay_as_anchor() -> None:
    schedule = CronTool._resolve_schedule(delay="2030-06-15T12:00:00+00:00", every="1h", until=None)
    assert isinstance(schedule, CronScheduleEvery)
    assert schedule.anchor.year == 2030


def test_resolve_schedule_with_until_clause() -> None:
    from datetime import datetime, timezone

    schedule = CronTool._resolve_schedule(
        delay=None, every="5m", until="2030-12-31T23:59:00+00:00"
    )
    assert isinstance(schedule, CronScheduleEvery)
    assert schedule.until == datetime(2030, 12, 31, 23, 59, tzinfo=timezone.utc)


def test_resolve_schedule_neither_raises() -> None:
    with pytest.raises(ValueError, match="either delay or every"):
        CronTool._resolve_schedule(delay=None, every=None, until=None)


# ---------------------------------------------------------------------------
# execute() action dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_add_creates_job_in_store(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        result = await tool.execute(ctx, action="add", message="ping", delay="5m")
        assert result.startswith("Created job")
        jobs = list(store.jobs())
        assert len(jobs) == 1
        assert jobs[0].message == "ping"
    finally:
        await store.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_execute_list_empty_returns_friendly_message(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        result = await tool.execute(ctx, action="list")
        assert result == "No scheduled jobs."
    finally:
        await store.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_execute_list_after_add_lists_one_job(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        await tool.execute(ctx, action="add", message="heartbeat", every="5m")
        result = await tool.execute(ctx, action="list")
        assert result.startswith("Scheduled jobs:")
        assert "every 5m" in result
    finally:
        await store.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_execute_remove_drops_job(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        result = await tool.execute(ctx, action="add", message="ping", delay="1h")
        # Created job 'XXXXXXXX' — extract id between quotes.
        jid = result.split("'")[1]
        result = await tool.execute(ctx, action="remove", job_id=jid)
        assert result == f"Removed job {jid}"
        assert list(store.jobs()) == []
    finally:
        await store.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_execute_unknown_action_raises(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        with pytest.raises(ValueError, match="Unknown action"):
            await tool.execute(ctx, action="bogus")
    finally:
        await store.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Error contracts surfaced to the agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_without_message_raises(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        with pytest.raises(ValueError, match="message is required"):
            await tool.execute(ctx, action="add", delay="5m")
    finally:
        await store.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_add_without_address_raises(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path)  # no address
        with pytest.raises(ValueError, match="no session context"):
            await tool.execute(ctx, action="add", message="ping", delay="5m")
    finally:
        await store.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_add_when_store_not_running_raises(tmp_path) -> None:
    bus = MessageBus()
    tool = CronTool(store_path=tmp_path / "jobs.json", bus=bus)
    ctx = ToolContext(workspace=tmp_path, address=_addr())
    with pytest.raises(RuntimeError, match="cron service not running"):
        await tool.execute(ctx, action="add", message="ping", delay="5m")


@pytest.mark.asyncio
async def test_list_when_store_not_running_raises(tmp_path) -> None:
    bus = MessageBus()
    tool = CronTool(store_path=tmp_path / "jobs.json", bus=bus)
    ctx = ToolContext(workspace=tmp_path, address=_addr())
    with pytest.raises(RuntimeError, match="cron service not running"):
        await tool.execute(ctx, action="list")


@pytest.mark.asyncio
async def test_remove_without_job_id_raises(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        with pytest.raises(ValueError, match="job_id is required"):
            await tool.execute(ctx, action="remove")
    finally:
        await store.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_remove_unknown_job_id_raises_keyerror(tmp_path) -> None:
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        with pytest.raises(KeyError, match="not found"):
            await tool.execute(ctx, action="remove", job_id="nonexistent")
    finally:
        await store.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Wakeup + execute_job edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_signals_wakeup_so_run_loop_re_evaluates(tmp_path) -> None:
    """Adding a job sets the wakeup event so the run_loop's bounded sleep
    breaks immediately — without this, a job scheduled mid-sleep would
    wait until the timer fires before being noticed."""
    tool, store, _bus = await _running_tool(tmp_path)
    try:
        tool._wakeup.clear()
        ctx = ToolContext(workspace=tmp_path, address=_addr())
        await tool.execute(ctx, action="add", message="ping", delay="5m")
        assert tool._wakeup.is_set()
    finally:
        await store.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_execute_job_without_bus_skips_quietly(tmp_path) -> None:
    tool = CronTool(store_path=tmp_path / "jobs.json", bus=None)
    job = CronJob(
        id="x",
        message="m",
        deliver_to=_addr(),
        schedule=CronScheduleEvery(every=timedelta(minutes=5)),
    )
    async with CronStore(tool._store_path) as store:
        tool._store = store
        store.add(job)
        # Should not raise even though there's no bus; the job is recorded
        # as executed and remains in the store as a recurring job.
        await tool._execute_job(job)
        assert store.get("x") is not None


@pytest.mark.asyncio
async def test_execute_job_bus_failure_logs_but_does_not_propagate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bus publish failure during execute is caught and logged so a
    transient delivery error doesn't take the cron loop down."""
    bus = MessageBus()

    async def boom(*_a, **_kw):
        raise RuntimeError("bus is unhappy")

    monkeypatch.setattr(bus, "publish_inbound", boom)
    tool = CronTool(store_path=tmp_path / "jobs.json", bus=bus)
    job = CronJob(
        id="x",
        message="m",
        deliver_to=_addr(),
        schedule=CronScheduleEvery(every=timedelta(minutes=5)),
    )
    async with CronStore(tool._store_path) as store:
        tool._store = store
        store.add(job)
        # Must not raise.
        await tool._execute_job(job)
        # Job stays scheduled (it's recurring), and last_run_at was stamped.
        assert store.get("x") is not None
        assert store.get("x").state.last_run_at is not None


# ---------------------------------------------------------------------------
# Tool schema surface
# ---------------------------------------------------------------------------


def test_tool_parameters_pydantic_schema_includes_required_fields() -> None:
    tool = CronTool(store_path=__file__, bus=None)  # path doesn't matter here
    schema = tool.parameters
    properties = schema["properties"]
    assert "action" in properties
    assert "message" in properties
    assert "delay" in properties
    assert "every" in properties
    # `action` has no default so Pydantic marks it required.
    assert "action" in schema.get("required", [])


def test_tool_terminal_when_lone_default_is_false() -> None:
    tool = CronTool(store_path=__file__, bus=None)
    assert tool.terminal_when_lone is False


def test_tool_name_is_cron() -> None:
    tool = CronTool(store_path=__file__, bus=None)
    assert tool.name == "cron"
