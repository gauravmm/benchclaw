"""Cron service for scheduling agent tasks."""

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from nanobot.agent.tools.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronStore,
)


def _now_s() -> int:
    return int(time.time())


def _compute_next_run(schedule: CronSchedule, now_s: int) -> int | None:
    """Compute next run time in seconds."""
    if schedule.kind == "at":
        return schedule.at_s if schedule.at_s and schedule.at_s > now_s else None

    if schedule.kind == "every":
        if not schedule.every_s or schedule.every_s <= 0:
            return None
        return now_s + schedule.every_s

    if schedule.kind == "cron" and schedule.expr:
        try:
            from croniter import croniter

            cron = croniter(schedule.expr, time.time())
            return int(cron.get_next())
        except Exception:
            return None

    return None


class CronStoreFile:
    """Wraps the cron store file; only writes when contents change."""

    def __init__(self, path: Path):
        self._path = path
        self._store: CronStore | None = None
        self._saved_json: str | None = None

    @property
    def store(self) -> CronStore:
        if self._store is None:
            if self._path.exists():
                try:
                    text = self._path.read_text()
                    self._store = CronStore.from_json(text)
                    self._saved_json = text
                except Exception as e:
                    logger.warning(f"Failed to load cron store: {e}")
                    self._store = CronStore()
            else:
                self._store = CronStore()
        assert self._store is not None
        return self._store

    def flush(self) -> None:
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
        self.store_path = store_path
        self.on_job = on_job  # Callback to execute job, returns response text
        self._store_file = CronStoreFile(store_path)
        self._timer_task: asyncio.Task | None = None
        self._running = False

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        now = _now_s()
        for job in self._store_file.store.jobs:
            if job.enabled:
                job.state.next_run_at_s = _compute_next_run(job.schedule, now)

    def _get_next_wake_s(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        times = [
            j.state.next_run_at_s
            for j in self._store_file.store.jobs
            if j.enabled and j.state.next_run_at_s
        ]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()

        next_wake = self._get_next_wake_s()
        if not next_wake or not self._running:
            return

        delay_s = max(0, next_wake - _now_s())

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        now = _now_s()
        due_jobs = [
            j
            for j in self._store_file.store.jobs
            if j.enabled and j.state.next_run_at_s and now >= j.state.next_run_at_s
        ]

        for job in due_jobs:
            await self._execute_job(job)

        self._store_file.flush()
        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
        start_s = _now_s()
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

        job.state.last_run_at_s = start_s
        job.updated_at_s = _now_s()

        # Handle one-shot jobs
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store_file.store.jobs = [
                    j for j in self._store_file.store.jobs if j.id != job.id
                ]
            else:
                job.enabled = False
                job.state.next_run_at_s = None
        else:
            job.state.next_run_at_s = _compute_next_run(job.schedule, _now_s())

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
        return sorted(jobs, key=lambda j: j.state.next_run_at_s or float("inf"))

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
        now = _now_s()

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
            state=CronJobState(next_run_at_s=_compute_next_run(schedule, now)),
            created_at_s=now,
            updated_at_s=now,
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
                job.updated_at_s = _now_s()
                if enabled:
                    job.state.next_run_at_s = _compute_next_run(job.schedule, _now_s())
                else:
                    job.state.next_run_at_s = None
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
            "next_wake_at_s": self._get_next_wake_s(),
        }


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cron_store.json"
        sf = CronStoreFile(path)

        # Add a test job
        now = _now_s()
        sf.store.jobs.append(
            CronJob(
                id="test01",
                name="Test job",
                schedule=CronSchedule(kind="every", every_s=60),
                payload=CronPayload(kind="agent_turn", message="hello"),
                state=CronJobState(next_run_at_s=now + 60),
                created_at_s=now,
                updated_at_s=now,
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
        sf2 = CronStoreFile(path)
        job = sf2.store.jobs[0]
        assert job.id == "test01"
        assert job.state.last_status == "ok"
        assert isinstance(job.created_at_s, int)
        assert isinstance(job.state.next_run_at_s, int)
        print(f"=== Round-trip OK: created_at_s={job.created_at_s}, next_run_at_s={job.state.next_run_at_s} ===")
        print("All checks passed.")
