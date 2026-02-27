"""Cron service for scheduling agent tasks."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Iterable, List

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
    """Wraps the cron store file; only writes when contents change."""

    def __init__(self, path: Path):
        self._path = path
        self._dirty: bool = False
        self._store: dict[str, CronJob]
        self._queue: heapdict[str, datetime]  # type: ignore

        try:
            data = CronData.from_json(self._path.read_text())
            self._store = {j.id: j for j in data.jobs}
            # TODO: Compute the priority queue for this
        except IOError as e:
            logger.warning(f"Failed to load cron store: {e}")
            self._store = {}

    def jobs(self) -> Iterable[CronJob]:
        # TODO: List all jobs in Store
        pass

    def add(self, j: CronJob) -> None:
        # TODO Add (or replace) CronJob
        pass

    def remove(self, jid: str) -> None:
        # TODO: Remove CronJob by ID
        pass

    def executed(self, jid: str):
        # Mark the job as executed. Process the job, deleting it if required.
        pass

    def flush(self) -> None:
        # TODO: Remove this
        """Write to disk only if contents changed."""
        if self._store is None:
            return
        current = self._store.to_json(indent=2)
        if current != self._saved_json:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(current)
            self._saved_json = current


class CronService:
    """Service for managing and executing scheduled jobs."""

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
    ):
        self.on_job = on_job  # Callback to execute job, returns response text
        self._store_file = CronStore(store_path)
        self._timer_task: asyncio.Task | None = None
        self._running = False

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        now = _now()
        for job in self._store_file.store.jobs:
            if job.enabled:
                job.state.next_run_at = job.schedule.next_run(now)

    def _get_next_wake(self) -> datetime | None:
        """Get the earliest next run time across all jobs."""
        times = [
            j.state.next_run_at
            for j in self._store_file.store.jobs
            if j.enabled and j.state.next_run_at
        ]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()

        next_wake = self._get_next_wake()
        if not next_wake or not self._running:
            return

        delay_s = max(0.0, (next_wake - _now()).total_seconds())

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        now = _now()
        due_jobs = [
            j
            for j in self._store_file.store.jobs
            if j.enabled and j.state.next_run_at and now >= j.state.next_run_at
        ]

        for job in due_jobs:
            await self._execute_job(job)

        self._store_file.flush()
        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
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

        # Handle one-shot jobs
        if isinstance(job.schedule, CronScheduleAt):
            if job.delete_after_run:
                self._store_file.store.jobs = [
                    j for j in self._store_file.store.jobs if j.id != job.id
                ]
            else:
                job.enabled = False
                job.state.next_run_at = None
        else:
            job.state.next_run_at = job.schedule.next_run(_now())

    # ========== Public API ==========

    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        _ = self._store_file.store  # ensure loaded
        self._recompute_next_runs()
        self._store_file.flush()
        self._arm_timer()
        logger.info(f"Cron service started with {len(self._store_file.store.jobs)} jobs")

    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        jobs = self._store_file.store.jobs
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
        delete_after_run: bool = False,
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
            delete_after_run=delete_after_run,
        )

        self._store_file.store.jobs.append(job)
        self._store_file.flush()
        self._arm_timer()

        logger.info(f"Cron: added job '{name}' ({job.id})")
        return job

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        store = self._store_file.store
        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id]
        removed = len(store.jobs) < before

        if removed:
            self._store_file.flush()
            self._arm_timer()
            logger.info(f"Cron: removed job {job_id}")

        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        for job in self._store_file.store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at = _now()
                if enabled:
                    job.state.next_run_at = job.schedule.next_run(_now())
                else:
                    job.state.next_run_at = None
                self._store_file.flush()
                self._arm_timer()
                return job
        return None

    def get_job(self, job_id: str) -> CronJob | None:
        """Get a job by ID."""
        for job in self._store_file.store.jobs:
            if job.id == job_id:
                return job
        return None

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        for job in self._store_file.store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._store_file.flush()
                self._arm_timer()
                return True
        return False

    def status(self) -> dict:
        """Get service status."""
        return {
            "enabled": self._running,
            "jobs": len(self._store_file.store.jobs),
            "next_wake_at": self._get_next_wake(),
        }


if __name__ == "__main__":
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cron_store.json"
        sf = CronStore(path)

        # Add a test job
        now = _now()
        sf.store.jobs.append(
            CronJob(
                id="test01",
                name="Test job",
                schedule=CronScheduleEvery(every=timedelta(seconds=60)),
                payload=CronPayload(kind="agent_turn", message="hello"),
                state=CronJobState(next_run_at=now + timedelta(seconds=60)),
                created_at=now,
                updated_at=now,
            )
        )

        # First flush — should write
        mtime_before = path.stat().st_mtime if path.exists() else None
        sf.flush()
        mtime_after = path.stat().st_mtime
        assert mtime_before != mtime_after, "Expected file to be written on first flush"

        print("=== Stored JSON ===")
        print(path.read_text())

        # Second flush without changes — should NOT write
        mtime_before = path.stat().st_mtime
        time.sleep(0.01)
        sf.flush()
        mtime_after = path.stat().st_mtime
        assert mtime_before == mtime_after, "Expected no write when data unchanged"
        print("=== No-op flush: OK ===")

        # Mutate and flush — should write
        sf.store.jobs[0].state.last_status = "ok"
        mtime_before = path.stat().st_mtime
        time.sleep(0.01)
        sf.flush()
        mtime_after = path.stat().st_mtime
        assert mtime_before != mtime_after, "Expected write after mutation"
        print("=== Mutation flush: OK ===")

        # Round-trip: load from file into new CronStoreFile
        sf2 = CronStore(path)
        job = sf2.store.jobs[0]
        assert job.id == "test01"
        assert job.state.last_status == "ok"
        assert isinstance(job.created_at, datetime)
        assert isinstance(job.state.next_run_at, datetime)
        print(
            f"=== Round-trip OK: created_at={job.created_at}, next_run_at={job.state.next_run_at} ==="
        )
        print("All checks passed.")
