"""Regression tests for CronStore serialization round-trip.

Covers three bugs found via the __main__ test block:
- _decode_schedule used "cron" instead of "expr" to detect CronScheduleCron
- CronPayload.deliver_to lacked encoder/decoder metadata (dataclasses_json skipped it)
- CronStore.__aenter__ called _ts_now() (returns a Field) instead of datetime.now()
"""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from benchclaw.agent.tools.cron.typesupport import (
    CronJob,
    CronJobState,
    CronPayload,
    CronScheduleAt,
    CronScheduleCron,
    CronScheduleEvery,
    CronStore,
)
from benchclaw.bus import MessageAddress


def _address() -> MessageAddress:
    return MessageAddress(channel="telegram", chat_id="123456")


def _store_path(tmp: str) -> Path:
    return Path(tmp) / "jobs.json"


@pytest.mark.asyncio
async def test_round_trip_all_schedule_types() -> None:
    """Writing then reading back all three schedule types preserves values."""
    address = _address()
    at_time = datetime(2026, 6, 1, 10, 0).astimezone()

    jobs_in = [
        CronJob(
            id="aaa00001",
            name="cron job",
            schedule=CronScheduleCron(expr="0 9 * * 1-5"),
            payload=CronPayload(message="standup", deliver_to=address),
            state=CronJobState(),
        ),
        CronJob(
            id="aaa00002",
            name="interval job",
            schedule=CronScheduleEvery(every=timedelta(minutes=30)),
            payload=CronPayload(message="heartbeat", deliver_to=address),
            state=CronJobState(),
        ),
        CronJob(
            id="aaa00003",
            name="one-time job",
            schedule=CronScheduleAt(at=at_time),
            payload=CronPayload(message="deadline", deliver_to=address),
            state=CronJobState(),
        ),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        path = _store_path(tmp)

        async with CronStore(path) as store:
            for job in jobs_in:
                store.add(job)

        async with CronStore(path) as store:
            by_id = {j.id: j for j in store.jobs()}

    assert len(by_id) == 3

    # Regression: _decode_schedule matched "cron" not "expr" for CronScheduleCron
    assert isinstance(by_id["aaa00001"].schedule, CronScheduleCron)
    assert by_id["aaa00001"].schedule.expr == "0 9 * * 1-5"

    assert isinstance(by_id["aaa00002"].schedule, CronScheduleEvery)
    assert by_id["aaa00002"].schedule.every == timedelta(minutes=30)

    assert isinstance(by_id["aaa00003"].schedule, CronScheduleAt)
    assert by_id["aaa00003"].schedule.at == at_time


@pytest.mark.asyncio
async def test_round_trip_deliver_to() -> None:
    """deliver_to address is preserved across serialization.

    Regression: CronPayload.deliver_to lacked dataclasses_json encoder/decoder metadata.
    """
    address = MessageAddress(channel="discord", chat_id="987654")
    job = CronJob(
        id="bbb00001",
        name="test",
        schedule=CronScheduleEvery(every=timedelta(hours=1)),
        payload=CronPayload(message="ping", deliver_to=address),
        state=CronJobState(),
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = _store_path(tmp)

        async with CronStore(path) as store:
            store.add(job)

        async with CronStore(path) as store:
            jobs = list(store.jobs())

    assert len(jobs) == 1
    assert jobs[0].payload.deliver_to.channel == "discord"
    assert jobs[0].payload.deliver_to.chat_id == "987654"


@pytest.mark.asyncio
async def test_aenter_does_not_call_ts_now() -> None:
    """CronStore.__aenter__ must not pass a Field object to schedule.next_run.

    Regression: __aenter__ called _ts_now() (returns dataclasses.Field) instead of
    datetime.now().astimezone(), causing TypeError in next_run computations.
    """
    job = CronJob(
        id="ccc00001",
        name="interval",
        schedule=CronScheduleEvery(every=timedelta(minutes=5)),
        payload=CronPayload(message="tick", deliver_to=_address()),
        state=CronJobState(),
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = _store_path(tmp)

        async with CronStore(path) as store:
            store.add(job)

        # This __aenter__ would raise TypeError if _ts_now() bug is present
        async with CronStore(path) as store:
            jobs = list(store.jobs())

    assert len(jobs) == 1
