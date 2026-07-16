"""Scheduler service: fires cron-defined agent launches unattended.

Schedules are event-sourced like every other core catalogue: ``schedule.created``
carries the full definition, ``schedule.deleted`` retires it, and
``schedule.triggered`` records each firing (with the launched session id, or the
contained error). On boot :meth:`rebuild` folds the log and recomputes each
schedule's next run from *now* — runs missed while the server was down are
skipped, not backfilled (launching a stale agent run hours late is worse than
waiting for the next slot).

Launching goes through an injected :class:`SessionLauncher` (the Adapter
Manager satisfies it structurally); the scheduler knows nothing about adapters
beyond their names. Launch failures are contained: they become an ``error``
field on the ``schedule.triggered`` event, never an unhandled exception in the
scheduler loop.
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from typing import Protocol

import structlog
from ulid import ULID

from prodeo.adapters.interface import LaunchSpec
from prodeo.bus.interface import EventBus
from prodeo.errors import InvalidScheduleError, UnknownScheduleError
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.persistence.interface import EventQuery, EventStore
from prodeo.scheduler.cron import next_fire, parse_cron
from prodeo.scheduler.model import Schedule
from prodeo.sessions.model import Session

_log = structlog.get_logger(__name__)

_SOURCE = "scheduler"

#: Upper bound on one loop sleep, so newly relevant wall-clock changes
#: (suspend/resume, NTP jumps) are noticed reasonably promptly.
_POLL_CAP_S = 30.0


class SessionLauncher(Protocol):
    """The one core capability the scheduler needs (the Adapter Manager)."""

    async def launch(self, adapter_name: str, spec: LaunchSpec) -> Session: ...


class SchedulerService:
    """In-memory schedule catalogue, event-sourced, single event loop."""

    def __init__(
        self,
        bus: EventBus,
        launcher: SessionLauncher,
        *,
        node: str = "local",
        tz: tzinfo | None = None,
        poll_cap_s: float = _POLL_CAP_S,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._bus = bus
        self._launcher = launcher
        self._node = node
        self._tz = tz or datetime.now(UTC).astimezone().tzinfo or UTC
        self._poll_cap_s = poll_cap_s
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._schedules: dict[str, Schedule] = {}
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------- queries

    def list_schedules(self) -> list[Schedule]:
        """All schedules, oldest first (ULIDs sort chronologically)."""
        return sorted(self._schedules.values(), key=lambda s: s.id)

    def get(self, schedule_id: str) -> Schedule | None:
        return self._schedules.get(schedule_id)

    # ------------------------------------------------------------ commands

    async def create(self, *, name: str, cron: str, adapter: str, spec: LaunchSpec) -> Schedule:
        """Define a schedule, publishing ``schedule.created``.

        Raises :class:`InvalidScheduleError` for an unparsable or never-firing
        cron expression — misconfiguration is rejected at the door, not
        discovered at 3am.
        """
        try:
            parsed = parse_cron(cron)
        except ValueError as exc:
            raise InvalidScheduleError(str(exc)) from exc
        now = self._now()
        first = next_fire(parsed, now, self._tz)
        if first is None:
            raise InvalidScheduleError(f"cron expression never fires: {cron!r}")
        schedule = Schedule(
            id=str(ULID()),
            name=name,
            cron=parsed.expression,
            adapter=adapter,
            spec=spec,
            created_at=now,
            next_run_at=first,
        )
        self._schedules[schedule.id] = schedule
        await self._bus.publish(
            new_event(
                ev.SCHEDULE_CREATED,
                node=self._node,
                source=_SOURCE,
                payload={"schedule": schedule.model_dump(mode="json")},
            )
        )
        self._wake.set()
        _log.info(
            "scheduler.created",
            schedule_id=schedule.id,
            name=name,
            cron=parsed.expression,
            next_run_at=first.isoformat(),
        )
        return schedule

    async def trigger_now(self, schedule_id: str) -> Schedule:
        """Fire a schedule immediately (manual run), same path as the cron loop."""
        schedule = self._schedules.get(schedule_id)
        if schedule is None:
            raise UnknownScheduleError(schedule_id)
        await self._fire(schedule, self._now())
        return schedule

    async def delete(self, schedule_id: str) -> None:
        """Retire a schedule, publishing ``schedule.deleted``."""
        if schedule_id not in self._schedules:
            raise UnknownScheduleError(schedule_id)
        del self._schedules[schedule_id]
        await self._bus.publish(
            new_event(
                ev.SCHEDULE_DELETED,
                node=self._node,
                source=_SOURCE,
                payload={"schedule_id": schedule_id},
            )
        )
        self._wake.set()
        _log.info("scheduler.deleted", schedule_id=schedule_id)

    # ------------------------------------------------------------- rebuild

    async def rebuild(self, store: EventStore) -> None:
        """Fold the persisted ``schedule.*`` log; recompute next runs from now."""
        cursor: str | None = None
        count = 0
        while True:
            batch = await store.query(
                EventQuery(after_id=cursor, type_pattern="schedule.*", limit=500)
            )
            if not batch:
                break
            for event in batch:
                self._apply(event)
                count += 1
            cursor = batch[-1].id
        now = self._now()
        for schedule in self._schedules.values():
            schedule.next_run_at = next_fire(parse_cron(schedule.cron), now, self._tz)
        _log.info("scheduler.rebuilt", events=count, schedules=len(self._schedules))

    def _apply(self, event: Event) -> None:
        payload = event.payload
        if event.type == ev.SCHEDULE_CREATED:
            try:
                schedule = Schedule.model_validate(payload["schedule"])
                parse_cron(schedule.cron)
            except (KeyError, ValueError) as exc:
                _log.warning("scheduler.orphan_event", event_id=event.id, error=str(exc))
                return
            self._schedules[schedule.id] = schedule
        elif event.type == ev.SCHEDULE_DELETED:
            self._schedules.pop(str(payload.get("schedule_id", "")), None)
        elif event.type == ev.SCHEDULE_TRIGGERED:
            schedule_or_none = self._schedules.get(str(payload.get("schedule_id", "")))
            if schedule_or_none is not None:
                schedule_or_none.last_triggered_at = event.timestamp

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="scheduler")
        _log.info("scheduler.started", schedules=len(self._schedules))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            now = self._now()
            for schedule in list(self._schedules.values()):
                if schedule.next_run_at is not None and schedule.next_run_at <= now:
                    await self._fire(schedule, now)
            self._wake.clear()
            timeout = self._sleep_seconds(self._now())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)

    def _sleep_seconds(self, now: datetime) -> float:
        upcoming = [s.next_run_at for s in self._schedules.values() if s.next_run_at is not None]
        if not upcoming:
            return self._poll_cap_s
        delta = (min(upcoming) - now).total_seconds()
        return min(max(delta, 0.0), self._poll_cap_s)

    async def _fire(self, schedule: Schedule, now: datetime) -> None:
        """Launch one scheduled run; failures land in the event, not the loop."""
        schedule.last_triggered_at = now
        schedule.next_run_at = next_fire(parse_cron(schedule.cron), now, self._tz)
        payload: dict[str, object] = {
            "schedule_id": schedule.id,
            "name": schedule.name,
            "adapter": schedule.adapter,
        }
        session_id: str | None = None
        try:
            session = await self._launcher.launch(schedule.adapter, schedule.spec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("scheduler.launch_failed", schedule_id=schedule.id)
            payload["error"] = str(exc)
        else:
            session_id = session.id
            payload["session_id"] = session.id
        await self._bus.publish(
            new_event(
                ev.SCHEDULE_TRIGGERED,
                node=self._node,
                source=_SOURCE,
                session_id=session_id,
                payload=payload,
            )
        )
        _log.info(
            "scheduler.triggered",
            schedule_id=schedule.id,
            name=schedule.name,
            session_id=session_id,
        )
