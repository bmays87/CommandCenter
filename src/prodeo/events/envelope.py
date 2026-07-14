"""The common event envelope shared by every event in the system."""

import threading
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

_id_lock = threading.Lock()
_last_id = 0


def _new_id() -> str:
    """A monotonic ULID.

    Cursor semantics (WebSocket dedup, ``after`` pagination) require ids from
    this process to be strictly increasing even within one millisecond, so ties
    are broken by incrementing the previous id's random component.
    """
    global _last_id
    with _id_lock:
        candidate = int(ULID())
        if candidate <= _last_id:
            candidate = _last_id + 1
        _last_id = candidate
        return str(ULID.from_int(candidate))


def _now() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    """Immutable envelope for a single event.

    ``id`` is a ULID, so lexicographic order is chronological order - clients
    use it as a cursor when reconciling after a disconnect.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=_new_id)
    type: str
    version: int = 1
    timestamp: datetime = Field(default_factory=_now)
    node: str = "local"
    session_id: str | None = None
    correlation_id: str | None = None
    source: str = "core"
    payload: dict[str, Any] = Field(default_factory=dict)


def new_event(
    type_: str,
    *,
    payload: dict[str, Any] | None = None,
    node: str = "local",
    source: str = "core",
    session_id: str | None = None,
    correlation_id: str | None = None,
    version: int = 1,
) -> Event:
    """Convenience constructor enforcing envelope defaults."""
    return Event(
        type=type_,
        version=version,
        node=node,
        source=source,
        session_id=session_id,
        correlation_id=correlation_id,
        payload=payload or {},
    )
