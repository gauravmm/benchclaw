"""Cron tool for scheduling reminders and tasks."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.cron.service import CronService
from nanobot.agent.tools.cron.typesupport import (
    CronJob,
    CronScheduleAt,
    CronScheduleCron,
    CronScheduleEvery,
)
from nanobot.bus import MessageBus, OutboundMessage

# The heartbeat cron job fires periodically and asks the agent to read HEARTBEAT.md.
HEARTBEAT_JOB_ID = "__heartbeat__"
HEARTBEAT_INTERVAL_S = 30 * 60
HEARTBEAT_PROMPT = """Read HEARTBEAT.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
If nothing needs attention, reply with just: HEARTBEAT_OK"""


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(
        self,
        store_path: Path,
        process_direct: Callable[..., Coroutine[Any, Any, str]],
        bus: MessageBus,
    ):
        self._cron = CronService(store_path)
        self._process_direct = process_direct
        self._bus = bus
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def skill(self) -> str | None:
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

    def _ensure_heartbeat(self) -> None:
        """Register the heartbeat cron job if it doesn't already exist."""
        if self._cron.get_job(HEARTBEAT_JOB_ID) is not None:
            return
        self._cron.add_job(
            name="Heartbeat",
            schedule=CronScheduleEvery(every=timedelta(seconds=HEARTBEAT_INTERVAL_S)),
            message=HEARTBEAT_PROMPT,
            job_id=HEARTBEAT_JOB_ID,
        )

    async def background(self) -> None:
        """Start the cron service and keep it running until cancelled."""

        async def on_job(job: CronJob) -> str | None:
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
            return response

        self._cron.on_job = on_job
        await self._cron.start()
        self._ensure_heartbeat()
        try:
            await asyncio.Event().wait()
        finally:
            await self._cron.stop()

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

        # Build schedule
        if every_seconds:
            schedule = CronScheduleEvery(every=timedelta(seconds=every_seconds))
        elif cron_expr:
            schedule = CronScheduleCron(expr=cron_expr)
        elif at:
            schedule = CronScheduleAt(at=datetime.fromisoformat(at))
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        job = self._cron.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=self._channel,
            to=self._chat_id,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
