"""SQLite EventStore: the shared contract suite plus SQLite-specific behavior."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from prodeo.events import new_event
from prodeo.persistence import EventQuery, SqliteEventStore
from prodeo.persistence.testing import EventStoreContractSuite


class TestSqliteContract(EventStoreContractSuite):
    """The full EventStore contract (ADR-0003) against SQLite."""

    @pytest_asyncio.fixture
    async def store(self, tmp_path: Path) -> AsyncIterator[SqliteEventStore]:
        s = SqliteEventStore(tmp_path / "events.db")
        await s.open()
        yield s
        await s.close()


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


@pytest.mark.asyncio
async def test_query_before_open_raises(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "events.db")
    with pytest.raises(RuntimeError):
        await store.query(EventQuery())
