"""Cron types."""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from dataclasses_json import DataClassJsonMixin, config


def _encode_ts(ts_s: int | None) -> str | None:
    if ts_s is None:
        return None
    return datetime.fromtimestamp(ts_s).astimezone().isoformat(timespec="seconds")


def _decode_ts(s: str | None) -> int | None:
    if s is None:
        return None
    return int(datetime.fromisoformat(s).timestamp())


def _ts(default: int | None = None):
    return field(default=default, metadata=config(encoder=_encode_ts, decoder=_decode_ts))


def _ts_now():
    return field(
        default_factory=lambda: int(time.time()),
        metadata=config(encoder=_encode_ts, decoder=_decode_ts),
    )


@dataclass
class CronSchedule(DataClassJsonMixin):
    """Schedule definition for a cron job."""

    kind: Literal["at", "every", "cron"]
    # For "at": absolute timestamp in seconds
    at_s: int | None = _ts()
    # For "every": interval in seconds
    every_s: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload(DataClassJsonMixin):
    """What to do when the job runs."""

    kind: Literal["system_event", "agent_turn", "heartbeat"] = "agent_turn"
    message: str = ""
    # Deliver response to channel
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number


@dataclass
class CronJobState(DataClassJsonMixin):
    """Runtime state of a job."""

    next_run_at_s: int | None = _ts()
    last_run_at_s: int | None = _ts()
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class CronJob(DataClassJsonMixin):
    """A scheduled job."""

    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_s: int = field(
        default_factory=lambda: int(time.time()),
        metadata=config(encoder=_encode_ts, decoder=_decode_ts),
    )
    updated_at_s: int = field(
        default_factory=lambda: int(time.time()),
        metadata=config(encoder=_encode_ts, decoder=_decode_ts),
    )
    delete_after_run: bool = False


@dataclass
class CronStore(DataClassJsonMixin):
    """Persistent store for cron jobs."""

    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
