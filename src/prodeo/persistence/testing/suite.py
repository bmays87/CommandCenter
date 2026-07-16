"""The EventStore contract tests (ADR-0003).

Every storage backend must pass this suite. Usage, in a backend's tests:

    import pytest_asyncio
    from prodeo.persistence.testing import EventStoreContractSuite

    class TestMyBackendContract(EventStoreContractSuite):
        @pytest_asyncio.fixture
        async def store(self):  # opened and torn down per test
            s = MyBackendStore(...)
            await s.open()
            yield s
            await s.close()

Backend-specific behavior (durability across reopen, storage details) belongs
in the backend's own tests alongside the subclass.
"""

from datetime import UTC, datetime

import pytest

from prodeo.events import Event, new_event
from prodeo.persistence.interface import EventQuery, EventStore


class EventStoreContractSuite:
    """Inherit and provide a ``store`` fixture; the tests come for free."""

    @pytest.fixture
    def store(self) -> EventStore:
        raise NotImplementedError("contract subclasses must provide a `store` fixture")

    # ------------------------------------------------------------ the suite

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_every_envelope_field(self, store: EventStore) -> None:
        event = Event(
            type="session.started",
            version=3,
            timestamp=datetime(2026, 7, 16, 12, 30, 45, 123456, tzinfo=UTC),
            node="workstation-01",
            session_id="cc-1",
            correlation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            source="adapter:claude-code",
            payload={"nested": {"list": [1, "two", None], "flag": True}, "text": "héllo"},
        )
        await store.append(event)

        (restored,) = await store.query(EventQuery())
        assert restored == event
        assert restored.timestamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_append_is_idempotent_per_event_id(self, store: EventStore) -> None:
        event = new_event("system.started")
        await store.append(event)
        await store.append(event)  # at-least-once delivery must not duplicate
        assert len(await store.query(EventQuery())) == 1

    @pytest.mark.asyncio
    async def test_ascending_ulid_order_with_exclusive_after_cursor(
        self, store: EventStore
    ) -> None:
        events = [new_event("tool.started", payload={"i": i}) for i in range(5)]
        for e in reversed(events):  # insert out of order on purpose
            await store.append(e)

        all_rows = await store.query(EventQuery())
        assert [e.id for e in all_rows] == sorted(e.id for e in events)

        after_two = await store.query(EventQuery(after_id=events[1].id))
        assert [e.payload["i"] for e in after_two] == [2, 3, 4]

    @pytest.mark.asyncio
    async def test_descending_order_with_exclusive_before_cursor(self, store: EventStore) -> None:
        events = [new_event("tool.started", payload={"i": i}) for i in range(5)]
        for e in events:
            await store.append(e)

        newest_first = await store.query(EventQuery(order="desc", limit=2))
        assert [e.payload["i"] for e in newest_first] == [4, 3]

        older = await store.query(EventQuery(order="desc", before_id=newest_first[-1].id))
        assert [e.payload["i"] for e in older] == [2, 1, 0]

    @pytest.mark.asyncio
    async def test_filters_by_pattern_session_and_limit(self, store: EventStore) -> None:
        await store.append(new_event("session.started", session_id="a"))
        await store.append(new_event("session.stopped", session_id="a"))
        await store.append(new_event("tool.started", session_id="b"))

        sessions = await store.query(EventQuery(type_pattern="session.*"))
        assert [e.type for e in sessions] == ["session.started", "session.stopped"]

        exact = await store.query(EventQuery(type_pattern="session.stopped"))
        assert [e.type for e in exact] == ["session.stopped"]

        only_b = await store.query(EventQuery(session_id="b"))
        assert [e.type for e in only_b] == ["tool.started"]

        limited = await store.query(EventQuery(limit=1))
        assert len(limited) == 1

    @pytest.mark.asyncio
    async def test_cursor_pagination_walks_the_whole_log_without_gaps(
        self, store: EventStore
    ) -> None:
        events = [new_event("agent.output_appended", payload={"i": i}) for i in range(25)]
        for e in events:
            await store.append(e)

        seen: list[int] = []
        cursor: str | None = None
        while True:
            batch = await store.query(EventQuery(after_id=cursor, limit=10))
            if not batch:
                break
            seen.extend(int(e.payload["i"]) for e in batch)
            cursor = batch[-1].id
        assert seen == list(range(25))

    @pytest.mark.asyncio
    async def test_empty_store_and_no_match_return_empty(self, store: EventStore) -> None:
        assert await store.query(EventQuery()) == []
        await store.append(new_event("tool.started"))
        assert await store.query(EventQuery(type_pattern="voice.*")) == []

    @pytest.mark.asyncio
    async def test_delete_removes_by_id_and_is_idempotent(self, store: EventStore) -> None:
        events = [new_event("agent.output_appended", payload={"i": i}) for i in range(3)]
        for e in events:
            await store.append(e)

        removed = await store.delete([events[0].id, events[2].id])
        assert removed == 2
        remaining = await store.query(EventQuery())
        assert [e.id for e in remaining] == [events[1].id]

        # Deleting the same (or unknown) ids again removes nothing.
        assert await store.delete([events[0].id, "01UNKNOWNULID0000000000000"]) == 0
        assert await store.delete([]) == 0
        assert len(await store.query(EventQuery())) == 1
