"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

from datetime import timedelta

from nanobot.agent.tools.cron.types import CronPayload, CronSchedule

if TYPE_CHECKING:
    from nanobot.agent.tools.cron.service import CronService

# Default interval: 30 minutes
DEFAULT_HEARTBEAT_INTERVAL_S = 30 * 60

# Reserved cron job ID for the heartbeat
HEARTBEAT_JOB_ID = "__heartbeat__"

# The prompt sent to agent during heartbeat
HEARTBEAT_PROMPT = """Read HEARTBEAT.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
If nothing needs attention, reply with just: HEARTBEAT_OK"""

# Token that indicates "nothing to do"
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"


def _is_heartbeat_empty(content: str | None) -> bool:
    """Check if HEARTBEAT.md has no actionable content."""
    if not content:
        return True

    # Lines to skip: empty, headers, HTML comments, empty checkboxes
    skip_patterns = {"- [ ]", "* [ ]", "- [x]", "* [x]"}

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("<!--") or line in skip_patterns:
            continue
        return False  # Found actionable content

    return True


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Scheduling is delegated to CronService. Call register() to set up the
    recurring cron job; the gateway's on_job callback routes heartbeat payloads
    back to tick().
    """

    def __init__(
        self,
        workspace: Path,
        on_heartbeat: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        enabled: bool = True,
    ):
        self.workspace = workspace
        self.on_heartbeat = on_heartbeat
        self.interval_s = interval_s
        self.enabled = enabled

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        """Read HEARTBEAT.md content."""
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text()
            except Exception:
                return None
        return None

    def register(self, cron_service: CronService) -> None:
        """Register (or re-enable) the heartbeat as a cron job."""
        if not self.enabled:
            logger.info("Heartbeat disabled; skipping registration")
            return

        existing = cron_service.get_job(HEARTBEAT_JOB_ID)
        if existing is not None:
            if not existing.enabled:
                cron_service.enable_job(HEARTBEAT_JOB_ID, enabled=True)
                logger.info("Heartbeat cron job re-enabled")
            else:
                logger.info("Heartbeat cron job already registered")
            return

        cron_service.add_job(
            name="Heartbeat",
            schedule=CronSchedule(kind="every", every=timedelta(seconds=self.interval_s)),
            message="",
            payload=CronPayload(kind="heartbeat"),
            job_id=HEARTBEAT_JOB_ID,
        )
        logger.info(f"Heartbeat registered as cron job (every {self.interval_s}s)")

    async def tick(self) -> None:
        """Execute a single heartbeat tick. Called by the cron on_job callback."""
        content = self._read_heartbeat_file()

        if _is_heartbeat_empty(content):
            logger.debug("Heartbeat: no tasks (HEARTBEAT.md empty)")
            return

        logger.info("Heartbeat: checking for tasks...")

        if self.on_heartbeat:
            try:
                response = await self.on_heartbeat(HEARTBEAT_PROMPT)

                if HEARTBEAT_OK_TOKEN.replace("_", "") in response.upper().replace("_", ""):
                    logger.info("Heartbeat: OK (no action needed)")
                else:
                    logger.info("Heartbeat: completed task")

            except Exception as e:
                logger.error(f"Heartbeat execution failed: {e}")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        if self.on_heartbeat:
            return await self.on_heartbeat(HEARTBEAT_PROMPT)
        return None
