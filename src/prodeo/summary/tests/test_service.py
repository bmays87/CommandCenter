"""Summary Service: digest folding, prose containment, publication."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from prodeo.bus import InProcessEventBus
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.persistence import SqliteEventStore
from prodeo.sessions import SessionDescriptor, SessionRegistry
from prodeo.summary import SummaryService
from prodeo.summary.service import _min_ulid_at

NOW = datetime(2026, 7, 16, 18, 0, tzinfo=UTC)


def _aged(type_: str, hours_old: float, payload: dict[str, object] | None = None) -> Event:
    at = NOW - timedelta(hours=hours_old)
    return new_event(type_, payload=dict(payload or {})).model_copy(
        update={"id": _min_ulid_at(at), "timestamp": at}
    )


class GoodSummarizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "good"

    async def summarize(self, instructions: str, content: str) -> str:
        self.calls.append((instructions, content))
        return "All quiet; two sessions finished."


class BrokenSummarizer:
    @property
    def name(self) -> str:
        return "broken"

    async def summarize(self, instructions: str, content: str) -> str:
        raise ConnectionError("ollama is down")


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteEventStore]:
    s = SqliteEventStore(tmp_path / "events.db")
    await s.open()
    yield s
    await s.close()


def _service(
    bus: InProcessEventBus, store: SqliteEventStore, registry: SessionRegistry | None = None
) -> SummaryService:
    return SummaryService(
        bus,
        store,
        registry or SessionRegistry(bus),
        cron="0 18 * * *",
        tz=UTC,
        node="test",
        now_fn=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_digest_folds_the_period_and_publishes(store: SqliteEventStore) -> None:
    bus = InProcessEventBus()
    for event in [
        _aged(ev.SESSION_COMPLETED, 2, {"title": "fix the tests"}),
        _aged(ev.SESSION_FAILED, 3, {"title": "deploy", "reason": "boom"}),
        _aged(ev.INTERACTION_REQUESTED, 4),
        _aged(ev.INTERACTION_ANSWERED, 4.01),
        _aged(ev.SCHEDULE_TRIGGERED, 5, {"name": "nightly", "session_id": "s1"}),
        _aged(ev.SCHEDULE_TRIGGERED, 6, {"name": "flaky", "error": "no adapter"}),
        _aged(ev.SYSTEM_RETENTION_COMPLETED, 7, {"events_deleted": 42}),
        _aged(ev.SESSION_COMPLETED, 30, {"title": "too old to count"}),  # outside window
    ]:
        await store.append(event)
    registry = SessionRegistry(bus)
    await registry.upsert_discovered("a", SessionDescriptor(native_id="live"))

    service = _service(bus, store, registry)
    event = await service.run_once()

    stats = event.payload["stats"]
    assert stats["sessions_completed"] == 1
    assert stats["sessions_failed"] == 1
    assert stats["interactions_requested"] == 1
    assert stats["interactions_answered"] == 1
    assert stats["schedule_triggers"] == 2
    assert stats["schedule_trigger_failures"] == 1
    assert stats["events_expired"] == 42
    assert stats["sessions_active"] == 1

    digest = event.payload["digest"]
    assert "fix the tests" in digest
    assert "deploy" in digest
    assert "nightly" in digest
    assert "too old to count" not in digest
    assert event.type == ev.SUMMARY_GENERATED
    assert event.payload["summarizer"] is None
    assert event.payload["prose"] == ""


@pytest.mark.asyncio
async def test_summarizer_prose_is_included(store: SqliteEventStore) -> None:
    bus = InProcessEventBus()
    await store.append(_aged(ev.SESSION_COMPLETED, 1, {"title": "t"}))
    service = _service(bus, store)
    summarizer = GoodSummarizer()
    service.set_summarizer(summarizer)

    event = await service.run_once()

    assert event.payload["prose"] == "All quiet; two sessions finished."
    assert event.payload["summarizer"] == "good"
    (instructions, content) = summarizer.calls[0]
    assert "digest" in instructions
    assert "Sessions:" in content


@pytest.mark.asyncio
async def test_summarizer_failure_is_contained(store: SqliteEventStore) -> None:
    bus = InProcessEventBus()
    service = _service(bus, store)
    service.set_summarizer(BrokenSummarizer())

    event = await service.run_once()

    assert event.payload["prose"] == ""
    assert event.payload["summarizer_error"] == "ollama is down"


@pytest.mark.asyncio
async def test_window_does_not_overlap_previous_run(store: SqliteEventStore) -> None:
    bus = InProcessEventBus()
    service = _service(bus, store)
    await store.append(_aged(ev.SESSION_COMPLETED, 1, {"title": "counted once"}))

    first = await service.run_once()
    second = await service.run_once()  # immediately after: empty window

    assert first.payload["stats"]["sessions_completed"] == 1
    assert second.payload["stats"]["sessions_completed"] == 0


@pytest.mark.asyncio
async def test_disabled_without_cron(store: SqliteEventStore) -> None:
    bus = InProcessEventBus()
    service = SummaryService(bus, store, SessionRegistry(bus), cron="", node="test")
    await service.start()  # no task, no error
    await service.stop()
