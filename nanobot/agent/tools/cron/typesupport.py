"""Cron types."""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal

from dataclasses_json import DataClassJsonMixin, config
from heapdict import heapdict
from loguru import logger

if TYPE_CHECKING:
    from nanobot.bus import MessageAddress


def _encode_ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone().isoformat(timespec="seconds")


def _decode_ts(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _encode_td(td: timedelta) -> float:
    return td.total_seconds()


def _decode_td(s: float) -> timedelta:
    return timedelta(seconds=s)


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

    at: datetime | None = _ts()

    def next_run(self, dt: datetime) -> datetime | None:
        return self.at if self.at and self.at > dt else None


@dataclass
class CronScheduleEvery(DataClassJsonMixin):
    """Run repeatedly with a fixed interval, anchored to a starting time."""

    every: timedelta = field(
        default=timedelta(hours=1), metadata=config(encoder=_encode_td, decoder=_decode_td)
    )
    anchor: datetime = _ts_now()

    def next_run(self, dt: datetime) -> datetime | None:
        if self.every <= timedelta(0):
            return None
        elapsed_s = (dt - self.anchor).total_seconds()
        n = math.floor(elapsed_s / self.every.total_seconds()) + 1
        return self.anchor + n * self.every


@dataclass
class CronScheduleCron(DataClassJsonMixin):
    """Run on a cron expression schedule."""

    expr: str = ""  # required; empty string is invalid
    tz: str = ""  # IANA timezone name; empty = local timezone

    def next_run(self, dt: datetime) -> datetime | None:
        if not self.expr:
            return None
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter

            start = dt.astimezone(ZoneInfo(self.tz)) if self.tz else dt.astimezone()
            cron = croniter(self.expr, start)
            return cron.get_next(datetime).astimezone()
        except Exception:
            return None


CronSchedule = CronScheduleAt | CronScheduleEvery | CronScheduleCron


def _encode_schedule(s: CronScheduleAt | CronScheduleEvery | CronScheduleCron) -> dict:
    return s.to_dict()


def _decode_schedule(d: dict) -> CronScheduleAt | CronScheduleEvery | CronScheduleCron:
    if "at" in d:
        return CronScheduleAt.from_dict(d)
    if "every" in d:
        return CronScheduleEvery.from_dict(d)
    if "cron" in d:
        return CronScheduleCron.from_dict(d)
    raise ValueError(f"Unknown schedule kind: {', '.join(d.keys())}")


def _encode_address(addr: "MessageAddress | None") -> dict | None:
    return None if addr is None else {"channel": addr.channel, "chat_id": addr.chat_id}


def _decode_address(d: dict | None) -> "MessageAddress | None":
    if d is None:
        return None
    from nanobot.bus import MessageAddress

    return MessageAddress(**d)


@dataclass
class CronPayload(DataClassJsonMixin):
    """What to do when the job runs."""

    message: str
    # Deliver response back to address where job was created
    deliver_to: MessageAddress


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


@dataclass
class CronData(DataClassJsonMixin):
    """Persistent store for cron jobs."""

    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)


class CronStore:
    """Async context manager: loads on enter, always writes back on exit."""

    def __init__(self, path: Path):
        self._path = path
        self._store: dict[str, CronJob] = {}
        self._queue: heapdict = heapdict()  # jid → next_run_at

    async def __aenter__(self) -> "CronStore":
        try:
            data = CronData.from_json(self._path.read_text())
            now = _ts_now()
            for j in data.jobs:
                self._store[j.id] = j
                if j.enabled and (next_run := j.schedule.next_run(now)) is not None:
                    self._queue[j.id] = next_run
                    j.state.next_run_at = next_run
        except IOError as e:
            logger.warning(f"No cron store at {e}. Creating one from scratch.")
        return self

    async def __aexit__(self, *_) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(CronData(jobs=list(self._store.values())).to_json(indent=2))

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

    def remove(self, jid: str) -> bool:
        """Remove a job by ID. Returns True if it existed."""
        if jid not in self._store:
            return False
        del self._store[jid]
        if jid in self._queue:
            del self._queue[jid]

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
