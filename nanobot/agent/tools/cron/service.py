"""Cron service for scheduling agent tasks."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Iterable

from heapdict import heapdict
from loguru import logger

from nanobot.agent.tools.cron.typesupport import (
    CronData,
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronScheduleAt,
    CronScheduleEvery,
)

_MAX_DT = datetime.max.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now().astimezone()


class CronStore:
    """Async context manager: loads on enter, writes back on exit if dirty."""

    def __init__(self, path: Path):
        self._path = path
        self._store: dict[str, CronJob] = {}
        self._queue: heapdict = heapdict()  # jid → next_run_at
        self._dirty = False

    async def __aenter__(self) -> "CronStore":
        try:
            data = CronData.from_json(self._path.read_text())
            now = _now()
            for j in data.jobs:
                self._store[j.id] = j
                if j.enabled:
                    next_run = j.schedule.next_run(now)
                    j.state.next_run_at = next_run
                    if next_run is not None:
                        self._queue[j.id] = next_run
        except IOError as e:
            logger.warning(f"Failed to load cron store: {e}")
        return self

    async def __aexit__(self, *_) -> None:
        if self._dirty:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(CronData(jobs=list(self._store.values())).to_json(indent=2))
            self._dirty = False

    def jobs(self) -> Iterable[CronJob]:
        return self._store.values()

    def get(self, jid: str) -> CronJob | None:
        return self._store.get(jid)

    def add(self, j: CronJob) -> None:
        """Add or replace a job and update the queue."""
        self._store[j.id] = j
        if j.enabled and j.state.next_run_at is not None:
            self._queue[j.id] = j.state.next_run_at
        elif j.id in self._queue:
            del self._queue[j.id]
        self._dirty = True

    def remove(self, jid: str) -> bool:
        """Remove a job by ID. Returns True if it existed."""
        if jid not in self._store:
            return False
        del self._store[jid]
        if jid in self._queue:
            del self._queue[jid]
        self._dirty = True
        return True

    def enable(self, jid: str, enabled: bool, now: datetime) -> bool:
        """Enable or disable a job. Returns False if job not found."""
        if (job := self._store.get(jid)) is None:
            return False
        job.enabled = enabled
        job.updated_at = now
        if enabled:
            next_run = job.schedule.next_run(now)
            job.state.next_run_at = next_run
            if next_run is not None:
                self._queue[jid] = next_run
            elif jid in self._queue:
                del self._queue[jid]
        else:
            job.state.next_run_at = None
            if jid in self._queue:
                del self._queue[jid]
        self._dirty = True
        return True

    def executed(self, jid: str, now: datetime) -> None:
        """Post-execution: remove CronScheduleAt jobs, reschedule recurring ones."""
        if (job := self._store.get(jid)) is None:
            return
        if isinstance(job.schedule, CronScheduleAt):
            self.remove(jid)
        else:
            next_run = job.schedule.next_run(now)
            job.state.next_run_at = next_run
            if next_run is not None:
                self._queue[jid] = next_run
            elif jid in self._queue:
                del self._queue[jid]
            self._dirty = True

    def next_wake(self) -> datetime | None:
        """Return the earliest scheduled next_run_at, or None."""
        if not self._queue:
            return None
        _, dt = self._queue.peekitem()
        return dt

    def pop_due(self, now: datetime) -> list[CronJob]:
        """Remove and return all jobs due at or before now."""
        due = []
        while self._queue:
            jid, next_run = self._queue.peekitem()
            if next_run > now:
                break
            self._queue.popitem()
            job = self._store.get(jid)
            if job is not None and job.enabled:
                due.append(job)
        return due


class CronService:
    """Service for managing and executing scheduled jobs."""

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
    ):
        self.on_job = on_job
        self._store = CronStore(store_path)
        self._timer_task: asyncio.Task | None = None
        self._running = False

    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()

        next_wake = self._store.next_wake()
        if not next_wake or not self._running:
            return

        delay_s = max(0.0, (next_wake - _now()).total_seconds())

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Handle timer tick — run due jobs."""
        now = _now()
        for job in self._store.pop_due(now):
            await self._execute_job(job)
        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job and update its state."""
        start = _now()
        logger.info(f"Cron: executing job '{job.name}' ({job.id})")
        try:
            if self.on_job:
                await self.on_job(job)
            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info(f"Cron: job '{job.name}' completed")
        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error(f"Cron: job '{job.name}' failed: {e}")
        job.state.last_run_at = start
        job.updated_at = _now()
        self._store.executed(job.id, _now())

    # ========== Public API ==========

    async def start(self) -> None:
        """Start the cron service."""
        await self._store.__aenter__()
        self._running = True
        self._arm_timer()
        logger.info(f"Cron service started with {len(list(self._store.jobs()))} jobs")

    async def stop(self) -> None:
        """Stop the cron service and flush the store."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        await self._store.__aexit__(None, None, None)

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs, sorted by next run time."""
        jobs = list(self._store.jobs())
        if not include_disabled:
            jobs = [j for j in jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at or _MAX_DT)

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        payload: CronPayload | None = None,
        job_id: str | None = None,
    ) -> CronJob:
        """Add a new job."""
        now = _now()
        job = CronJob(
            id=job_id or str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=payload
            or CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
            ),
            state=CronJobState(next_run_at=schedule.next_run(now)),
            created_at=now,
            updated_at=now,
        )
        self._store.add(job)
        self._arm_timer()
        logger.info(f"Cron: added job '{name}' ({job.id})")
        return job

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        removed = self._store.remove(job_id)
        if removed:
            self._arm_timer()
            logger.info(f"Cron: removed job {job_id}")
        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        if not self._store.enable(job_id, enabled, _now()):
            return None
        self._arm_timer()
        return self._store.get(job_id)

    def get_job(self, job_id: str) -> CronJob | None:
        """Get a job by ID."""
        return self._store.get(job_id)

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        job = self._store.get(job_id)
        if job is None:
            return False
        if not force and not job.enabled:
            return False
        await self._execute_job(job)
        self._arm_timer()
        return True

    def status(self) -> dict:
        """Get service status."""
        return {
            "enabled": self._running,
            "jobs": len(list(self._store.jobs())),
            "next_wake_at": self._store.next_wake(),
        }


if __name__ == "__main__":
    import asyncio
    import tempfile

    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cron_store.json"

            # Test: add job, verify it's written on close
            async with CronStore(path) as store:
                now = _now()
                store.add(CronJob(
                    id="test01",
                    name="Test job",
                    schedule=CronScheduleEvery(every=timedelta(seconds=60)),
                    payload=CronPayload(kind="agent_turn", message="hello"),
                    state=CronJobState(next_run_at=now + timedelta(seconds=60)),
                    created_at=now,
                    updated_at=now,
                ))
            assert path.exists(), "Expected file written on close"
            print("=== Stored JSON ===")
            print(path.read_text())
            print("=== Write on close: OK ===")

            # Test: no write if not dirty
            mtime_before = path.stat().st_mtime
            async with CronStore(path) as _:
                pass
            mtime_after = path.stat().st_mtime
            assert mtime_before == mtime_after, "Expected no write when not dirty"
            print("=== No-op close: OK ===")

            # Test: round-trip
            async with CronStore(path) as store2:
                jobs2 = list(store2.jobs())
                assert len(jobs2) == 1
                job2 = jobs2[0]
                assert job2.id == "test01"
                assert isinstance(job2.created_at, datetime)
                assert isinstance(job2.state.next_run_at, datetime)
                print(
                    f"=== Round-trip OK: created_at={job2.created_at},"
                    f" next_run_at={job2.state.next_run_at} ==="
                )
            print("All checks passed.")

    asyncio.run(main())
