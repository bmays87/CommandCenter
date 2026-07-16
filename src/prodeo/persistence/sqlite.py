"""SQLite-backed EventStore (WAL mode, JSON payloads, ULID primary keys)."""

import json
from collections.abc import Sequence
from pathlib import Path

import aiosqlite

from prodeo.bus.interface import matches
from prodeo.events import Event
from prodeo.persistence.interface import EventQuery

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    version        INTEGER NOT NULL,
    timestamp      TEXT NOT NULL,
    node           TEXT NOT NULL,
    session_id     TEXT,
    correlation_id TEXT,
    source         TEXT NOT NULL,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type, id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id, id);
"""


class SqliteEventStore:
    """Default local-first event store."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store is not open")
        return self._db

    async def append(self, event: Event) -> None:
        db = self._conn()
        await db.execute(
            "INSERT OR IGNORE INTO events "
            "(id, type, version, timestamp, node, session_id, correlation_id, source, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.type,
                event.version,
                event.timestamp.isoformat(),
                event.node,
                event.session_id,
                event.correlation_id,
                event.source,
                json.dumps(event.payload),
            ),
        )
        await db.commit()

    async def query(self, q: EventQuery) -> list[Event]:
        db = self._conn()
        sql = (
            "SELECT id, type, version, timestamp, node, session_id,"
            " correlation_id, source, payload FROM events"
        )
        where: list[str] = []
        args: list[str] = []
        if q.after_id is not None:
            where.append("id > ?")
            args.append(q.after_id)
        if q.before_id is not None:
            where.append("id < ?")
            args.append(q.before_id)
        if q.session_id is not None:
            where.append("session_id = ?")
            args.append(q.session_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC" if q.order == "desc" else " ORDER BY id ASC"

        results: list[Event] = []
        async with db.execute(sql, args) as cursor:
            async for row in cursor:
                if not matches(q.type_pattern, str(row[1])):
                    continue
                results.append(
                    Event(
                        id=row[0],
                        type=row[1],
                        version=row[2],
                        timestamp=row[3],
                        node=row[4],
                        session_id=row[5],
                        correlation_id=row[6],
                        source=row[7],
                        payload=json.loads(row[8]),
                    )
                )
                if len(results) >= q.limit:
                    break
        return results

    async def delete(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        db = self._conn()
        removed = 0
        # Chunked to stay under SQLite's bound-parameter limit.
        for start in range(0, len(ids), 500):
            chunk = list(ids[start : start + 500])
            placeholders = ",".join("?" * len(chunk))
            cursor = await db.execute(f"DELETE FROM events WHERE id IN ({placeholders})", chunk)
            removed += cursor.rowcount
        await db.commit()
        return removed

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
