"""Cron types."""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal

from dataclasses_json import DataClassJsonMixin, config
from heapdict import heapdict
from loguru import logger

from benchclaw.bus import MessageAddress
from benchclaw.utils import DurationField

if TYPE_CHECKING:
    pass


def _encode_ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone().isoformat(timespec="seconds")


def _ensure_aware(dt: datetime) -> datetime:
    """Return dt with timezone; assumes system timezone if naive."""
    return dt if dt.tzinfo is not None else dt.astimezone()


def _decode_ts(s: str | None) -> datetime | None:
    return None if s is None else _ensure_aware(datetime.fromisoformat(s))


def _encode_unix_ts(ts: float | None) -> float | None:
    return ts


def _decode_unix_ts(value: float | int | str | datetime | None) -> float | None:
    """Decode numeric/legacy datetime inputs into a Unix timestamp."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value).timestamp()
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return _ensure_aware(datetime.fromisoformat(value)).timestamp()
    return float(value)


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

    def __str__(self):
        return f"at {self.at.isoformat(timespec='seconds') if self.at else 'never'}"


@dataclass
class CronScheduleEvery(DataClassJsonMixin):
    """Run repeatedly with a fixed interval, anchored to a starting time."""

    every: DurationField = timedelta(hours=1)
    anchor: datetime = _ts_now()
    until: datetime | None = _ts()

    def next_run(self, dt: datetime) -> datetime | None:
        if self.every <= timedelta(0):
            return None
        elapsed_s = (dt - self.anchor).total_seconds()
        n = math.floor(elapsed_s / self.every.total_seconds()) + 1
        t = self.anchor + n * self.every
        if self.until is not None and t > self.until:
            return None
        return t

    def __str__(self) -> str:
        s = int(self.every.total_seconds())
        if s % 3600 == 0:
            base = f"every {s // 3600}h"
        elif s % 60 == 0:
            base = f"every {s // 60}m"
        else:
            base = f"every {s}s"
        if self.until is not None:
            return f"{base} until {self.until.strftime('%Y-%m-%d %H:%M')}"
        return base


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

    def __str__(self) -> str:
        return f"cron '{self.expr}'" + (f" ({self.tz})" if self.tz else "")


CronSchedule = CronScheduleAt | CronScheduleEvery | CronScheduleCron


def _encode_schedule(s: CronScheduleAt | CronScheduleEvery | CronScheduleCron) -> dict:
    return s.to_dict()


def _decode_schedule(d: dict) -> CronScheduleAt | CronScheduleEvery | CronScheduleCron:
    if "at" in d:
        return CronScheduleAt.from_dict(d)
    if "every" in d:
        return CronScheduleEvery.from_dict(d)
    if "expr" in d:
        return CronScheduleCron.from_dict(d)
    raise ValueError(f"Unknown schedule kind: {', '.join(d.keys())}")


def _encode_address(addr: "MessageAddress | None") -> dict | None:
    return None if addr is None else {"channel": addr.channel, "chat_id": addr.chat_id}


def _decode_address(d: dict | None) -> "MessageAddress | None":
    if d is None:
        return None
    from benchclaw.bus import MessageAddress

    return MessageAddress(**d)


@dataclass
class CronJobState(DataClassJsonMixin):
    """Runtime state of a job."""

    last_run_at: float | None = field(
        default=None,
        metadata=config(encoder=_encode_unix_ts, decoder=_decode_unix_ts),
    )
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class CronJob(DataClassJsonMixin):
    """A scheduled job."""

    id: str
    message: str
    deliver_to: MessageAddress = field(
        default=None,  # type: ignore[assignment]
        metadata=config(encoder=_encode_address, decoder=_decode_address),
    )
    state: CronJobState = field(default_factory=CronJobState)
    enabled: bool = True
    schedule: CronScheduleAt | CronScheduleEvery | CronScheduleCron = field(
        default_factory=CronScheduleEvery,
        metadata=config(encoder=_encode_schedule, decoder=_decode_schedule),
    )
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
            now = datetime.now().astimezone()
            for j in data.jobs:
                next_run = j.schedule.next_run(now)
                if next_run is None:
                    continue  # expired; skip so it's dropped on next write
                self._store[j.id] = j
                if j.enabled:
                    self._queue[j.id] = next_run
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
        if j.enabled:
            next_run = j.schedule.next_run(datetime.now().astimezone())
            if next_run is not None:
                self._queue[j.id] = next_run
                return
        if j.id in self._queue:
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
            if next_run is not None:
                self._queue[jid] = next_run
            elif jid in self._queue:
                del self._queue[jid]
        else:
            if jid in self._queue:
                del self._queue[jid]

        return True

    def executed(self, jid: str, now: datetime) -> None:
        """Post-execution: remove one-shot or expired jobs, reschedule recurring ones."""
        if (job := self._store.get(jid)) is None:
            return
        if isinstance(job.schedule, CronScheduleAt):
            self.remove(jid)
            return
        next_run = job.schedule.next_run(now)
        if next_run is not None:
            self._queue[jid] = next_run
        else:
            self.remove(jid)  # auto-delete expired recurring jobs

    def next_run_for(self, jid: str) -> datetime | None:
        """Return the queued next run time for a specific job, or None."""
        return self._queue.get(jid)

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
            if job is None:
                logger.warning(
                    f"Cron: job '{jid}' was in queue but not in store (ghost entry); skipping"
                )
                continue
            if not job.enabled:
                logger.debug(f"Cron: job '{jid}' was due but is disabled; skipping")
                continue
            due.append(job)
        return due
