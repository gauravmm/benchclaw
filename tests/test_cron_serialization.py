import asyncio
import json
from datetime import datetime, timedelta

import pytest

from benchclaw.agent.tools.cron.tool import CronTool
from benchclaw.agent.tools.cron.typesupport import (
    CronJob,
    CronJobState,
    CronScheduleEvery,
    CronStore,
)
from benchclaw.bus import MessageAddress, MessageBus, SystemEvent


def _address() -> MessageAddress:
    return MessageAddress(channel="telegram", chat_id="123456")


@pytest.mark.asyncio
async def test_last_run_at_round_trips_as_timestamp(tmp_path) -> None:
    last_run_at = datetime(2026, 3, 10, 8, 56, 16).astimezone().timestamp()
    job = CronJob(
        id="aaa00001",
        message="heartbeat",
        deliver_to=_address(),
        schedule=CronScheduleEvery(every=timedelta(minutes=30)),
        state=CronJobState(last_run_at=last_run_at, last_status="ok"),
    )
    store_path = tmp_path / "jobs.json"

    async with CronStore(store_path) as store:
        store.add(job)

    data = json.loads(store_path.read_text())
    assert data["jobs"][0]["state"]["last_run_at"] == pytest.approx(last_run_at)

    async with CronStore(store_path) as store:
        jobs = list(store.jobs())

    assert len(jobs) == 1
    assert jobs[0].state.last_run_at == pytest.approx(last_run_at)


@pytest.mark.asyncio
async def test_last_run_at_accepts_legacy_iso_datetime(tmp_path) -> None:
    last_run = datetime(2026, 3, 10, 9, 14, 47).astimezone()
    timestamp = last_run.timestamp()
    iso = last_run.isoformat(timespec="seconds")
    store_path = tmp_path / "jobs.json"
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "bbb00001",
                        "message": "legacy",
                        "deliver_to": {"channel": "telegram", "chat_id": "123456"},
                        "state": {
                            "last_run_at": iso,
                            "last_status": "ok",
                            "last_error": None,
                        },
                        "enabled": True,
                        "schedule": {"every": 1800.0, "anchor": iso, "until": None},
                        "created_at": iso,
                        "updated_at": iso,
                    }
                ],
            }
        )
    )

    async with CronStore(store_path) as store:
        jobs = list(store.jobs())

    assert len(jobs) == 1
    assert jobs[0].state.last_run_at == pytest.approx(timestamp)


@pytest.mark.asyncio
async def test_execute_job_records_timestamp_state(tmp_path) -> None:
    bus = MessageBus()
    address = _address()
    bus.inbound[address] = asyncio.Queue()
    tool = CronTool(store_path=tmp_path / "jobs.json", bus=bus)
    job = CronJob(
        id="ccc00001",
        message="tick",
        deliver_to=address,
        schedule=CronScheduleEvery(every=timedelta(minutes=5)),
        state=CronJobState(),
    )

    async with CronStore(tool._store_path) as store:
        tool._store = store
        store.add(job)
        before = datetime.now().astimezone().timestamp()
        await tool._execute_job(job)
        after = datetime.now().astimezone().timestamp()

        event = await bus.consume_inbound(address=address)

        assert isinstance(event, SystemEvent)
        assert event.content == "tick"
        assert job.state.last_status == "ok"
        assert job.state.last_error is None
        assert job.state.last_run_at is not None
        assert before <= job.state.last_run_at <= after

    tool._store = None
