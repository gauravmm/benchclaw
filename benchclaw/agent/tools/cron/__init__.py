"""Cron tool package — schedules and runs agent tasks."""

from benchclaw.agent.tools.base import register_tool
from benchclaw.agent.tools.cron.tool import CronTool

register_tool("cron", CronTool)

__all__ = ["CronTool"]
