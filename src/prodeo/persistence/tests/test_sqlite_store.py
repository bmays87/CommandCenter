"""EventStore contract tests, run against the SQLite implementation.

Written as a reusable mixin so future backends (e.g. MongoDB, ADR-0003)
inherit the identical contract.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from prodeo.events import new_event
from prodeo.persistence import EventQuery, SqliteEventStore
from prodeo.persistence.interface import EventStore


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[EventStore]:
    s = SqliteEventStore(tmp_path / "events.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_append_and_query_roundtrip(store: EventStore) -> None:
    event = new_event("session.started", payload={"agent": "claude-code"}, session_id="cc-1")
    await store.append(event)

    results = await store.query(EventQuery())
    assert results == [event]


@pytest.mark.asyncio
async def test_query_is_ulid_ordered_with_cursor(store: EventStore) -> None:
    events = [new_event("tool.started", payload={"i": i}) for i in range(5)]
    for e in reversed(events):  # insert out of order on purpose
        await store.append(e)

    all_rows = await store.query(EventQuery())
    assert [e.id for e in all_rows] == sorted(e.id for e in events)

    after_two = await store.query(EventQuery(after_id=events[1].id))
    assert [e.payload["i"] for e in after_two] == [2, 3, 4]


@pytest.mark.asyncio
async def test_query_filters_by_pattern_session_and_limit(store: EventStore) -> None:
    await store.append(new_event("session.started", session_id="a"))
    await store.append(new_event("session.stopped", session_id="a"))
    await store.append(new_event("tool.started", session_id="b"))

    sessions = await store.query(EventQuery(type_pattern="session.*"))
    assert [e.type for e in sessions] == ["session.started", "session.stopped"]

    only_b = await store.query(EventQuery(session_id="b"))
    assert [e.type for e in only_b] == ["tool.started"]

    limited = await store.query(EventQuery(limit=1))
    assert len(limited) == 1


@pytest.mark.asyncio
async def test_append_is_idempotent_per_event_id(store: EventStore) -> None:
    event = new_event("system.started")
    await store.append(event)
    await store.append(event)  # at-least-once delivery must not duplicate
    assert len(await store.query(EventQuery())) == 1


@pytest.mark.asyncio
async def test_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    first = SqliteEventStore(path)
    await first.open()
    event = new_event("system.started")
    await first.append(event)
    await first.close()

    second = SqliteEventStore(path)
    await second.open()
    assert await second.query(EventQuery()) == [event]
    await second.close()
