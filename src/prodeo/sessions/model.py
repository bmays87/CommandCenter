"""Session domain models shared by the registry, adapters, and the API."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from prodeo.sessions.state import SessionState


def _now() -> datetime:
    return datetime.now(UTC)


class SessionDescriptor(BaseModel):
    """An adapter's description of one session it can observe.

    ``native_id`` is the agent's own identifier (e.g. a Claude Code session
    UUID); the registry maps (adapter, native_id) to a Command-Center-assigned
    session id.
    """

    native_id: str
    title: str = ""
    project: str = ""
    model: str | None = None
    state: SessionState = SessionState.RUNNING
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    """The registry's canonical record of one agent session."""

    id: str
    adapter: str
    native_id: str
    title: str = ""
    project: str = ""
    model: str | None = None
    state: SessionState = SessionState.DISCOVERED
    created_at: datetime = Field(default_factory=_now)
    last_activity_at: datetime = Field(default_factory=_now)
    ended_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
