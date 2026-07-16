"""Retention: expire high-volume events (archiving them first) and archive
long-finished sessions.

Retention is opt-in (``PRODEO_RETENTION_RULES``) and deliberately conservative:

- Rebuild-critical namespaces (``session.*``, ``schedule.*``, ``interaction.*``)
  are never deleted, whatever the rules say — the Session Registry, Scheduler,
  and Mediation Service reconstruct their catalogues from those events on boot.
  They are low-volume; the log's bulk is agent output and tool activity.
- Expired events are appended to monthly gzip JSONL archives
  (``archive/events-YYYY-MM.jsonl.gz``) *before* deletion, unless a rule opts
  out with ``archive: false``. Archives are plain event-envelope JSON, one per
  line — readable by anything, forever.
- Sessions that finished more than ``archive_sessions_after_days`` ago move to
  the ``archived`` state through the Session Registry (the normal state
  machine, the normal events); their own ``session.*`` history stays in the log.

Each pass that changes anything publishes ``system.retention_completed`` with
the counts, so dashboards and the daily summary can report on it. File IO runs
in threads (async discipline).
"""

import asyncio
import contextlib
import gzip
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from pydantic import BaseModel
from ulid import ULID

from prodeo.bus.interface import EventBus
from prodeo.errors import IllegalTransitionError
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.persistence.interface import EventQuery, EventStore
from prodeo.sessions.registry import SessionRegistry
from prodeo.sessions.state import SessionState

_log = structlog.get_logger(__name__)

_SOURCE = "retention"

#: Namespaces the core rebuilds state from; retention never deletes them.
PROTECTED_PREFIXES = ("session.", "schedule.", "interaction.")

_BATCH = 500


class RetentionRule(BaseModel):
    """One expiry rule: events matching ``types`` older than ``max_age_days``."""

    types: str = "*"  # event type pattern: exact, ``ns.*``, or ``*``
    max_age_days: float
    #: Write expired events to the gzip archive before deleting.
    archive: bool = True


def _min_ulid_at(cutoff: datetime) -> str:
    """The smallest ULID whose timestamp is ``cutoff`` (an exclusive cursor:
    every event strictly older sorts below it)."""
    ms = int(cutoff.timestamp() * 1000)
    return str(ULID.from_bytes(ms.to_bytes(6, "big") + bytes(10)))


class RetentionService:
    """Periodic retention passes over the event log and session catalogue."""

    def __init__(
        self,
        bus: EventBus,
        store: EventStore,
        registry: SessionRegistry,
        *,
        archive_dir: Path,
        rules: list[RetentionRule] | None = None,
        archive_sessions_after_days: float | None = None,
        interval_s: float = 3600.0,
        node: str = "local",
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._registry = registry
        self._archive_dir = archive_dir
        self._rules = rules or []
        self._archive_sessions_after_days = archive_sessions_after_days
        self._interval_s = interval_s
        self._node = node
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._rules) or self._archive_sessions_after_days is not None

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not self.enabled:
            _log.info("retention.disabled")
            return
        self._task = asyncio.create_task(self._run(), name="retention")
        _log.info("retention.started", rules=len(self._rules), interval_s=self._interval_s)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # Sleep first: a retention pass during boot would compete with
        # rebuilds and adapter catch-up for no urgency whatsoever.
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("retention.pass_failed")

    # ----------------------------------------------------------- one pass

    async def run_once(self) -> dict[str, int]:
        """One full retention pass; returns the counts it publishes."""
        now = self._now()
        deleted = archived = 0
        for rule in self._rules:
            d, a = await self._apply_rule(rule, now)
            deleted += d
            archived += a
        sessions_archived = await self._archive_sessions(now)
        counts = {
            "events_deleted": deleted,
            "events_archived": archived,
            "sessions_archived": sessions_archived,
        }
        if any(counts.values()):
            await self._bus.publish(
                new_event(
                    ev.SYSTEM_RETENTION_COMPLETED,
                    node=self._node,
                    source=_SOURCE,
                    payload=dict(counts),
                )
            )
            _log.info("retention.completed", **counts)
        return counts

    async def _apply_rule(self, rule: RetentionRule, now: datetime) -> tuple[int, int]:
        """Walk the expired window with a forward cursor, archiving then deleting."""
        cutoff_id = _min_ulid_at(now - timedelta(days=rule.max_age_days))
        cursor: str | None = None
        deleted = archived = 0
        while True:
            batch = await self._store.query(
                EventQuery(
                    after_id=cursor, before_id=cutoff_id, type_pattern=rule.types, limit=_BATCH
                )
            )
            if not batch:
                return deleted, archived
            cursor = batch[-1].id
            expendable = [e for e in batch if not e.type.startswith(PROTECTED_PREFIXES)]
            if expendable:
                if rule.archive:
                    await asyncio.to_thread(self._archive_events, expendable)
                    archived += len(expendable)
                deleted += await self._store.delete([e.id for e in expendable])

    def _archive_events(self, events: list[Event]) -> None:
        """Append events to per-month gzip JSONL archives (blocking; threaded)."""
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        by_month: dict[str, list[Event]] = {}
        for event in events:
            by_month.setdefault(event.timestamp.strftime("%Y-%m"), []).append(event)
        for month, batch in by_month.items():
            path = self._archive_dir / f"events-{month}.jsonl.gz"
            with gzip.open(path, "at", encoding="utf-8") as fh:
                for event in batch:
                    fh.write(event.model_dump_json() + "\n")

    async def _archive_sessions(self, now: datetime) -> int:
        if self._archive_sessions_after_days is None:
            return 0
        cutoff = now - timedelta(days=self._archive_sessions_after_days)
        count = 0
        for session in self._registry.list_sessions():
            if session.state is SessionState.ARCHIVED:
                continue
            ended = session.ended_at or session.last_activity_at
            if (
                session.state
                in (
                    SessionState.COMPLETED,
                    SessionState.FAILED,
                    SessionState.STOPPED,
                )
                and ended < cutoff
            ):
                with contextlib.suppress(IllegalTransitionError):
                    await self._registry.observe_state(
                        session.id, SessionState.ARCHIVED, reason="retention"
                    )
                    count += 1
        return count
