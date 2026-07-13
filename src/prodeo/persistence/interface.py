"""EventStore interface (ADR-0003: SQLite default, MongoDB optional plugin)."""

from typing import Protocol

from pydantic import BaseModel

from prodeo.events import Event


class EventQuery(BaseModel):
    """Cursor-style query over the event log."""

    after_id: str | None = None  # exclusive ULID cursor
    type_pattern: str = "*"  # exact, ``ns.*`` or ``*``
    session_id: str | None = None
    limit: int = 500


class EventStore(Protocol):
    """Append-only, ULID-ordered event log."""

    async def open(self) -> None: ...

    async def append(self, event: Event) -> None: ...

    async def query(self, q: EventQuery) -> list[Event]: ...

    async def close(self) -> None: ...
