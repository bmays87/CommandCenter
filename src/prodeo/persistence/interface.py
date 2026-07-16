"""EventStore interface (ADR-0003: SQLite default, MongoDB optional plugin)."""

from typing import Literal, Protocol

from pydantic import BaseModel

from prodeo.events import Event


class EventQuery(BaseModel):
    """Cursor-style query over the event log.

    ``after_id``/``order="asc"`` pages forward (live tail reconciliation);
    ``before_id``/``order="desc"`` pages backward from the newest event (the
    event explorer's "load older"). Both cursors are exclusive.
    """

    after_id: str | None = None  # exclusive ULID cursor
    before_id: str | None = None  # exclusive ULID cursor (paging backward)
    type_pattern: str = "*"  # exact, ``ns.*`` or ``*``
    session_id: str | None = None
    limit: int = 500
    order: Literal["asc", "desc"] = "asc"


class EventStore(Protocol):
    """Append-only, ULID-ordered event log."""

    async def open(self) -> None: ...

    async def append(self, event: Event) -> None: ...

    async def query(self, q: EventQuery) -> list[Event]: ...

    async def close(self) -> None: ...
