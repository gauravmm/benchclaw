"""Cron tool for scheduling reminders and tasks."""

import contextlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolBuildContext
from nanobot.agent.tools.cron.typesupport import (
    CronJob,
    CronJobState,
    CronPayload,
    CronScheduleAt,
    CronScheduleCron,
    CronScheduleEvery,
    CronStore,
    _ts_now,
)
from nanobot.bus import MessageBus, OutboundMessage

_MAX_DT = datetime.max.replace(tzinfo=timezone.utc)


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    master_only = True

    @classmethod
    def build(cls, config: None, ctx: ToolBuildContext) -> "CronTool":
        assert ctx.bus is not None
        return cls(
            store_path=ctx.workspace / "cron" / "jobs.json",
            process_direct=ctx.process_direct,
            bus=ctx.bus,
        )

    def __init__(
        self,
        store_path: Path,
        process_direct: Callable[..., Coroutine[Any, Any, str]],
        bus: MessageBus,
    ):
        self._store_path = store_path
        self._process_direct = process_direct
        self._bus = bus
        self._channel = ""
        self._chat_id = ""
        self._store: CronStore | None = None
        self._wakeup: Any = None  # asyncio.Event, set after loop starts

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Schedule reminders and recurring tasks. Actions: add, list, remove."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {"type": "string", "description": "Reminder message (for add)"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')",
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
            },
            "required": ["action"],
        }

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a job: run the agent turn and optionally deliver the response."""
        assert self._store is not None
        start = _ts_now()
        logger.info(f"Cron: executing job '{job.name}' ({job.id})")
        try:
            response = await self._process_direct(
                job.payload.message,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
            if job.payload.deliver and job.payload.to:
                await self._bus.publish_outbound(
                    OutboundMessage(
                        channel=job.payload.channel or "cli",
                        chat_id=job.payload.to,
                        content=response or "",
                    )
                )
            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info(f"Cron: job '{job.name}' completed")
        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error(f"Cron: job '{job.name}' failed: {e}")
        job.state.last_run_at = start
        job.updated_at = start
        self._store.executed(job.id, start)

    async def background(self) -> None:
        """Run the cron loop until cancelled."""
        import asyncio

        self._wakeup = asyncio.Event()
        try:
            async with CronStore(self._store_path) as store:
                self._store = store
                while True:
                    now = _ts_now()
                    for job in store.pop_due(now):
                        await self._execute_job(job)

                    next_wake = store.next_wake()
                    delay = max(0.0, (next_wake - now).total_seconds()) if next_wake else 60.0
                    self._wakeup.clear()
                    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                        await asyncio.wait_for(self._wakeup.wait(), timeout=delay)

        finally:
            self._store = None

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            return self._add_job(message, every_seconds, cron_expr, at)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self, message: str, every_seconds: int | None, cron_expr: str | None, at: str | None
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        if self._store is None:
            return "Error: cron service not running"

        if every_seconds:
            schedule = CronScheduleEvery(every=timedelta(seconds=every_seconds))
        elif cron_expr:
            schedule = CronScheduleCron(expr=cron_expr)
        elif at:
            schedule = CronScheduleAt(at=datetime.fromisoformat(at))
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        now = _ts_now()
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=message[:30],
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=True,
                channel=self._channel,
                to=self._chat_id,
            ),
            state=CronJobState(next_run_at=schedule.next_run(now)),
            created_at=now,
            updated_at=now,
        )
        self._store.add(job)
        assert self._wakeup is not None
        self._wakeup.set()
        return f"Created job '{job.name}' (id: {job.id})"

    def _list_jobs(self) -> str:
        if self._store is None:
            return "Error: cron service not running"
        jobs = sorted(
            [j for j in self._store.jobs() if j.enabled],
            key=lambda j: j.state.next_run_at or _MAX_DT,
        )
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._store is None:
            return "Error: cron service not running"
        if self._store.remove(job_id):
            assert self._wakeup is not None
            self._wakeup.set()
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
