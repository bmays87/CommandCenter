"""Summary Service: a cron-scheduled digest of what the fleet did.

Each run folds the event log for the period (default: since the previous run,
capped at the period length) into a compact statistics digest — sessions
completed/failed, interactions answered/timed out, scheduled runs, retention —
and, when a ``summarizer`` plugin is installed, asks it for a short prose
rendition. The result is published as ``summary.generated``; delivery to
humans is the Notifier's job (route ``summary.generated`` to a channel in
``PRODEO_NOTIFY_RULES``).

A summarizer failure or timeout never blocks the digest: the event still
publishes, with ``summarizer_error`` set and prose empty. The core stays
vendor-free — it knows the :class:`Summarizer` Protocol, nothing else.
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo

import structlog
from ulid import ULID

from prodeo.bus.interface import EventBus
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.persistence.interface import EventQuery, EventStore
from prodeo.scheduler.cron import next_fire, parse_cron
from prodeo.sessions.registry import SessionRegistry
from prodeo.sessions.state import SessionState
from prodeo.summary.interface import Summarizer

_log = structlog.get_logger(__name__)

_SOURCE = "summary"

#: One loop sleep is capped so clock jumps are noticed promptly.
_MAX_SLEEP_S = 300.0

_PROSE_INSTRUCTIONS = (
    "You are the daily briefing writer for a fleet of coding agents. "
    "Summarize the following activity digest in 3-5 plain sentences for the "
    "human operator: lead with outcomes and anything that needs attention "
    "(failures, unanswered questions), skip anything with a zero count, and "
    "do not invent details that are not in the digest."
)


def _min_ulid_at(cutoff: datetime) -> str:
    ms = int(cutoff.timestamp() * 1000)
    return str(ULID.from_bytes(ms.to_bytes(6, "big") + bytes(10)))


class SummaryService:
    """Builds and publishes periodic activity digests."""

    def __init__(
        self,
        bus: EventBus,
        store: EventStore,
        registry: SessionRegistry,
        *,
        cron: str = "",
        tz: tzinfo | None = None,
        node: str = "local",
        summarize_timeout_s: float = 120.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._registry = registry
        self._cron = cron.strip()
        self._tz = tz or datetime.now(UTC).astimezone().tzinfo or UTC
        self._node = node
        self._summarize_timeout_s = summarize_timeout_s
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._summarizer: Summarizer | None = None
        self._last_run_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    def set_summarizer(self, summarizer: Summarizer | None) -> None:
        """Wire the (optional) summarizer plugin; called by the composition root."""
        self._summarizer = summarizer

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not self._cron:
            _log.info("summary.disabled")
            return
        parse_cron(self._cron)  # fail loudly at boot, not at 6pm
        self._task = asyncio.create_task(self._run(), name="summary")
        _log.info(
            "summary.started",
            cron=self._cron,
            summarizer=self._summarizer.name if self._summarizer else None,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        spec = parse_cron(self._cron)
        while True:
            now = self._now()
            target = next_fire(spec, now, self._tz)
            if target is None:  # unsatisfiable expression: nothing to do, ever
                _log.warning("summary.cron_never_fires", cron=self._cron)
                return
            while (remaining := (target - self._now()).total_seconds()) > 0:
                await asyncio.sleep(min(remaining, _MAX_SLEEP_S))
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("summary.run_failed")

    # ------------------------------------------------------------ one run

    async def run_once(self, *, period_hours: float = 24.0) -> Event:
        """Build and publish one digest; returns the published event."""
        now = self._now()
        start = now - timedelta(hours=period_hours)
        if self._last_run_at is not None:
            start = max(start, self._last_run_at)
        self._last_run_at = now

        stats, lines = await self._fold(start, now)
        digest = self._render(start, now, stats, lines)
        prose = ""
        summarizer_error = ""
        if self._summarizer is not None:
            try:
                async with asyncio.timeout(self._summarize_timeout_s):
                    prose = await self._summarizer.summarize(_PROSE_INSTRUCTIONS, digest)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("summary.summarizer_failed", error=str(exc))
                summarizer_error = str(exc)

        payload = {
            "period_start": start.isoformat(),
            "period_end": now.isoformat(),
            "stats": stats,
            "digest": digest,
            "prose": prose,
            "summarizer": self._summarizer.name if self._summarizer else None,
            **({"summarizer_error": summarizer_error} if summarizer_error else {}),
        }
        event = new_event(ev.SUMMARY_GENERATED, node=self._node, source=_SOURCE, payload=payload)
        await self._bus.publish(event)
        _log.info("summary.generated", events=stats["events_total"], prose=bool(prose))
        return event

    async def _fold(
        self, start: datetime, end: datetime
    ) -> tuple[dict[str, int], dict[str, list[str]]]:
        """One pass over the period's events: counters plus notable titles."""
        counts = {
            "events_total": 0,
            "sessions_completed": 0,
            "sessions_failed": 0,
            "interactions_requested": 0,
            "interactions_answered": 0,
            "interactions_timed_out": 0,
            "schedule_triggers": 0,
            "schedule_trigger_failures": 0,
            "events_expired": 0,
        }
        lines: dict[str, list[str]] = {"completed": [], "failed": [], "scheduled": []}
        cursor: str | None = _min_ulid_at(start)
        end_id = _min_ulid_at(end)
        while True:
            batch = await self._store.query(
                EventQuery(after_id=cursor, before_id=end_id, limit=500)
            )
            if not batch:
                break
            cursor = batch[-1].id
            for event in batch:
                counts["events_total"] += 1
                self._tally(event, counts, lines)
        counts["sessions_active"] = sum(
            1
            for s in self._registry.list_sessions()
            if s.state in (SessionState.RUNNING, SessionState.WAITING_ON_USER)
        )
        return counts, lines

    @staticmethod
    def _tally(event: Event, counts: dict[str, int], lines: dict[str, list[str]]) -> None:
        payload = event.payload
        if event.type == ev.SESSION_COMPLETED:
            counts["sessions_completed"] += 1
            _note(lines["completed"], str(payload.get("title") or payload.get("project") or ""))
        elif event.type == ev.SESSION_FAILED:
            counts["sessions_failed"] += 1
            _note(lines["failed"], str(payload.get("title") or payload.get("project") or ""))
        elif event.type == ev.INTERACTION_REQUESTED:
            counts["interactions_requested"] += 1
        elif event.type == ev.INTERACTION_ANSWERED:
            counts["interactions_answered"] += 1
        elif event.type == ev.INTERACTION_TIMED_OUT:
            counts["interactions_timed_out"] += 1
        elif event.type == ev.SCHEDULE_TRIGGERED:
            counts["schedule_triggers"] += 1
            name = str(payload.get("name", ""))
            if "error" in payload:
                counts["schedule_trigger_failures"] += 1
                _note(lines["scheduled"], f"{name} (failed: {payload['error']})")
            else:
                _note(lines["scheduled"], name)
        elif event.type == ev.SYSTEM_RETENTION_COMPLETED:
            counts["events_expired"] += int(payload.get("events_deleted", 0))

    @staticmethod
    def _render(
        start: datetime, end: datetime, stats: dict[str, int], lines: dict[str, list[str]]
    ) -> str:
        def fmt(dt: datetime) -> str:
            return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

        out = [
            f"Fleet activity {fmt(start)} -> {fmt(end)}",
            f"Sessions: {stats['sessions_completed']} completed, "
            f"{stats['sessions_failed']} failed, {stats['sessions_active']} active now",
        ]
        if lines["completed"]:
            out.append("  completed: " + "; ".join(lines["completed"]))
        if lines["failed"]:
            out.append("  failed: " + "; ".join(lines["failed"]))
        out.append(
            f"Interactions: {stats['interactions_requested']} requested, "
            f"{stats['interactions_answered']} answered, "
            f"{stats['interactions_timed_out']} timed out"
        )
        if stats["schedule_triggers"]:
            out.append(
                f"Scheduled runs: {stats['schedule_triggers']} "
                f"({stats['schedule_trigger_failures']} failed): " + "; ".join(lines["scheduled"])
            )
        if stats["events_expired"]:
            out.append(f"Retention: {stats['events_expired']} events expired to archive")
        out.append(f"Total events: {stats['events_total']}")
        return "\n".join(out)


def _note(bucket: list[str], text: str, cap: int = 10) -> None:
    if text and len(bucket) < cap:
        bucket.append(text)
