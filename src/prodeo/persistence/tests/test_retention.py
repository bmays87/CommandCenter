"""Retention: expiry rules, archives, protected namespaces, session archival."""

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from prodeo.bus import InProcessEventBus
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.persistence import (
    EventQuery,
    RetentionRule,
    RetentionService,
    SqliteEventStore,
)
from prodeo.persistence.retention import _min_ulid_at
from prodeo.sessions import SessionDescriptor, SessionRegistry, SessionState

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _aged(type_: str, days_old: float, session_id: str | None = None) -> Event:
    """An event whose ULID (and timestamp) place it ``days_old`` days ago."""
    at = NOW - timedelta(days=days_old)
    return new_event(type_, session_id=session_id).model_copy(
        update={"id": _min_ulid_at(at), "timestamp": at}
    )


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteEventStore]:
    s = SqliteEventStore(tmp_path / "events.db")
    await s.open()
    yield s
    await s.close()


def _service(
    store: SqliteEventStore,
    tmp_path: Path,
    *,
    rules: list[RetentionRule] | None = None,
    archive_sessions_after_days: float | None = None,
    bus: InProcessEventBus | None = None,
    registry: SessionRegistry | None = None,
) -> RetentionService:
    bus = bus or InProcessEventBus()
    return RetentionService(
        bus,
        store,
        registry or SessionRegistry(bus),
        archive_dir=tmp_path / "archive",
        rules=rules,
        archive_sessions_after_days=archive_sessions_after_days,
        node="test",
        now_fn=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_expired_events_are_archived_then_deleted(
    store: SqliteEventStore, tmp_path: Path
) -> None:
    old = _aged("agent.output_appended", days_old=40, session_id="s1")
    fresh = _aged("agent.output_appended", days_old=5, session_id="s1")
    await store.append(old)
    await store.append(fresh)

    service = _service(store, tmp_path, rules=[RetentionRule(types="agent.*", max_age_days=30)])
    counts = await service.run_once()

    assert counts["events_deleted"] == 1
    assert counts["events_archived"] == 1
    remaining = await store.query(EventQuery())
    assert [e.id for e in remaining] == [fresh.id]

    month = old.timestamp.strftime("%Y-%m")
    archive = tmp_path / "archive" / f"events-{month}.jsonl.gz"
    with gzip.open(archive, "rt", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh]
    assert [line["id"] for line in lines] == [old.id]
    assert lines[0]["payload"] == old.payload


@pytest.mark.asyncio
async def test_archive_false_deletes_without_archiving(
    store: SqliteEventStore, tmp_path: Path
) -> None:
    await store.append(_aged("notification.sent", days_old=10))
    service = _service(
        store,
        tmp_path,
        rules=[RetentionRule(types="notification.*", max_age_days=7, archive=False)],
    )
    counts = await service.run_once()
    assert counts == {"events_deleted": 1, "events_archived": 0, "sessions_archived": 0}
    assert not (tmp_path / "archive").exists()


@pytest.mark.asyncio
async def test_rebuild_critical_namespaces_are_never_deleted(
    store: SqliteEventStore, tmp_path: Path
) -> None:
    protected = [
        _aged("session.discovered", days_old=100),
        _aged("schedule.created", days_old=101),
        _aged("interaction.requested", days_old=102),
    ]
    doomed = _aged("tool.started", days_old=103)
    for e in [*protected, doomed]:
        await store.append(e)

    # A greedy "*" rule still may not touch the protected namespaces.
    service = _service(store, tmp_path, rules=[RetentionRule(types="*", max_age_days=1)])
    counts = await service.run_once()

    assert counts["events_deleted"] == 1
    remaining = {e.id for e in await store.query(EventQuery())}
    assert remaining == {e.id for e in protected}
    assert doomed.id not in remaining


@pytest.mark.asyncio
async def test_pass_pages_through_large_windows(store: SqliteEventStore, tmp_path: Path) -> None:
    for i in range(1203):
        await store.append(
            _aged("agent.output_appended", days_old=40 + i * 0.001, session_id=f"s{i}")
        )
    service = _service(store, tmp_path, rules=[RetentionRule(types="agent.*", max_age_days=30)])
    counts = await service.run_once()
    assert counts["events_deleted"] == 1203
    assert await store.query(EventQuery()) == []


@pytest.mark.asyncio
async def test_completed_pass_publishes_fact_only_when_something_happened(
    store: SqliteEventStore, tmp_path: Path
) -> None:
    bus = InProcessEventBus()
    sub = bus.subscribe("system.*", name="probe")
    service = _service(
        store, tmp_path, rules=[RetentionRule(types="agent.*", max_age_days=30)], bus=bus
    )

    await service.run_once()  # empty store: nothing happened, no event
    await store.append(_aged("agent.output_appended", days_old=40))
    await service.run_once()

    events: list[Event] = []
    try:
        async with asyncio.timeout(0.05):
            async for event in sub:
                events.append(event)
    except TimeoutError:
        pass
    assert [e.type for e in events] == [ev.SYSTEM_RETENTION_COMPLETED]
    assert events[0].payload["events_deleted"] == 1


@pytest.mark.asyncio
async def test_old_finished_sessions_move_to_archived(
    store: SqliteEventStore, tmp_path: Path
) -> None:
    bus = InProcessEventBus()
    registry = SessionRegistry(bus)
    old_done = await registry.upsert_discovered("a", SessionDescriptor(native_id="old"))
    await registry.observe_state(old_done.id, SessionState.COMPLETED)
    old_done.ended_at = NOW - timedelta(days=20)
    fresh_done = await registry.upsert_discovered("a", SessionDescriptor(native_id="fresh"))
    await registry.observe_state(fresh_done.id, SessionState.COMPLETED)
    running = await registry.upsert_discovered("a", SessionDescriptor(native_id="run"))

    service = _service(store, tmp_path, archive_sessions_after_days=14, bus=bus, registry=registry)
    counts = await service.run_once()

    assert counts["sessions_archived"] == 1
    assert registry.get(old_done.id).state is SessionState.ARCHIVED  # type: ignore[union-attr]
    assert registry.get(fresh_done.id).state is SessionState.COMPLETED  # type: ignore[union-attr]
    assert registry.get(running.id).state is SessionState.RUNNING  # type: ignore[union-attr]


def test_disabled_when_unconfigured(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    service = RetentionService(
        bus,
        SqliteEventStore(tmp_path / "e.db"),
        SessionRegistry(bus),
        archive_dir=tmp_path / "archive",
    )
    assert service.enabled is False
