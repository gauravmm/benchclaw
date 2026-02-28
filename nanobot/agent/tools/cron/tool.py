"""Cron tool for scheduling reminders and tasks."""

import contextlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolContext
from nanobot.agent.tools.cron.typesupport import (
    CronJob,
    CronJobState,
    CronPayload,
    CronScheduleAt,
    CronScheduleCron,
    CronScheduleEvery,
    CronStore,
)
from nanobot.bus import InboundMessage, MessageAddress, MessageBus

_MAX_DT = datetime.max.replace(tzinfo=timezone.utc)


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    master_only = True

    @classmethod
    def build(cls, config: None, ctx: ToolContext) -> "CronTool":
        return cls(
            store_path=ctx.workspace / "cron" / "jobs.json",
            bus=ctx.bus,
        )

    def __init__(
        self,
        store_path: Path,
        bus: MessageBus | None,
    ):
        self._store_path = store_path
        self._bus = bus
        self._store: CronStore | None = None
        self._wakeup: Any = None  # asyncio.Event, set after loop starts

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
        """Execute a job: inject a synthetic inbound message to re-invoke the agent."""
        assert self._store is not None
        if self._bus is None:
            logger.warning(f"Cron: no bus configured, skipping job '{job.name}' ({job.id})")
            return
        start = datetime.now().astimezone()
        logger.info(f"Cron: executing job '{job.name}' ({job.id})")
        try:
            deliver_to = job.payload.deliver_to
            await self._bus.publish_inbound(
                InboundMessage(
                    address=MessageAddress(
                        channel=deliver_to.channel if deliver_to else "cli",
                        chat_id=deliver_to.chat_id if deliver_to else "cron",
                    ),
                    sender_id="cron",
                    content=job.payload.message,
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

    async def background(self, ctx: ToolContext) -> None:
        """Run the cron loop until cancelled."""
        import asyncio

        self._wakeup = asyncio.Event()
        try:
            async with CronStore(self._store_path) as store:
                self._store = store
                while True:
                    now = datetime.now().astimezone()
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
        ctx: ToolContext,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            return self._add_job(ctx.address, message, every_seconds, cron_expr, at)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        address: MessageAddress | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        at: str | None,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not address:
            return "Error: no session context (address)"
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

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=message[:30],
            schedule=schedule,
            payload=CronPayload(message=message, deliver_to=address),
            state=CronJobState(),
        )
        self._store.add(job)
        assert self._wakeup is not None
        self._wakeup.set()
        return f"Created job '{job.name}' (id: {job.id})"

    def _list_jobs(self) -> str:
        if self._store is None:
            return "Error: cron service not running"
        jobs = self._store.jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule})" for j in jobs]
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
