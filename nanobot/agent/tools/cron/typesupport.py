"""Cron types."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from dataclasses_json import DataClassJsonMixin, config


def _encode_ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone().isoformat(timespec="seconds")


def _decode_ts(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _encode_td(td: timedelta | None) -> float | None:
    return None if td is None else td.total_seconds()


def _decode_td(s: float | None) -> timedelta | None:
    return None if s is None else timedelta(seconds=s)


def _ts(default: datetime | None = None):
    return field(default=default, metadata=config(encoder=_encode_ts, decoder=_decode_ts))


def _ts_now():
    return field(
        default_factory=lambda: datetime.now().astimezone(),
        metadata=config(encoder=_encode_ts, decoder=_decode_ts),
    )


@dataclass
class CronScheduleAt(DataClassJsonMixin):
    """Run once at a specific datetime."""

    kind: Literal["at"] = "at"
    at: datetime | None = _ts()

    def next_run(self, dt: datetime) -> datetime | None:
        return self.at if self.at and self.at > dt else None


@dataclass
class CronScheduleEvery(DataClassJsonMixin):
    """Run repeatedly with a fixed interval."""

    kind: Literal["every"] = "every"
    every: timedelta = field(
        default=timedelta(hours=1), metadata=config(encoder=_encode_td, decoder=_decode_td)
    )
    # TODO: Compute the next run relative to a fixed starting time.

    def next_run(self, dt: datetime) -> datetime | None:
        if self.every <= timedelta(0):
            return None
        return dt + self.every


@dataclass
class CronScheduleCron(DataClassJsonMixin):
    """Run on a cron expression schedule."""

    kind: Literal["cron"] = "cron"
    # TODO: Don't support None value for expr or tz.
    expr: str | None = None
    tz: str | None = None

    def next_run(self, dt: datetime) -> datetime | None:
        if not self.expr:
            return None
        try:
            from croniter import croniter

            cron = croniter(self.expr, dt)
            return datetime.fromtimestamp(cron.get_next()).astimezone()
        except Exception:
            return None


CronSchedule = CronScheduleAt | CronScheduleEvery | CronScheduleCron


def _encode_schedule(s: CronScheduleAt | CronScheduleEvery | CronScheduleCron) -> dict:
    return s.to_dict()


def _decode_schedule(d: dict) -> CronScheduleAt | CronScheduleEvery | CronScheduleCron:
    kind = d.get("kind")
    if kind == "at":
        return CronScheduleAt.from_dict(d)
    if kind == "every":
        return CronScheduleEvery.from_dict(d)
    if kind == "cron":
        return CronScheduleCron.from_dict(d)
    raise ValueError(f"Unknown schedule kind: {kind!r}")


@dataclass
class CronPayload(DataClassJsonMixin):
    """What to do when the job runs."""

    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    # Deliver response to channel
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number


@dataclass
class CronJobState(DataClassJsonMixin):
    """Runtime state of a job."""

    next_run_at: datetime | None = _ts()
    last_run_at: datetime | None = _ts()
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class CronJob(DataClassJsonMixin):
    """A scheduled job."""

    id: str
    name: str
    enabled: bool = True
    schedule: CronScheduleAt | CronScheduleEvery | CronScheduleCron = field(
        default_factory=CronScheduleEvery,
        metadata=config(encoder=_encode_schedule, decoder=_decode_schedule),
    )
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at: datetime = _ts_now()
    updated_at: datetime = _ts_now()
    delete_after_run: bool = False


@dataclass
class CronData(DataClassJsonMixin):
    """Persistent store for cron jobs."""

    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
