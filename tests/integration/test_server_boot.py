"""Phase 0 exit criterion: the composed server boots, records, and shuts down."""

from pathlib import Path

import pytest

from prodeo.config import Settings
from prodeo.events import types as ev
from prodeo.persistence import EventQuery, SqliteEventStore
from prodeo.server import Server

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_server_lifecycle_records_system_events(tmp_path: Path) -> None:
    settings = Settings(node_name="test-node", data_dir=tmp_path)
    server = Server(settings)

    await server.start()
    await server.stop()

    store = SqliteEventStore(settings.event_db_path)
    await store.open()
    events = await store.query(EventQuery(type_pattern="system.*"))
    await store.close()

    assert [e.type for e in events] == [ev.SYSTEM_STARTED, ev.SYSTEM_STOPPING]
    assert all(e.node == "test-node" for e in events)
    assert events[0].payload["version"]
