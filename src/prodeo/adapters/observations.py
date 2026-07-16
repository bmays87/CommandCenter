"""Typed observations adapters report through ``AdapterContext.report``.

Adapters never publish events. They report observations keyed by their own
``native_id``; the Adapter Manager validates them, resolves Command Center
session identity, and translates them into domain events. A buggy adapter can
therefore emit garbage without corrupting the event stream.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from prodeo.mediation.model import InteractionKind
from prodeo.sessions.model import SessionDescriptor
from prodeo.sessions.state import SessionState


class SessionObservation(BaseModel):
    """A session exists (or its descriptive fields changed)."""

    descriptor: SessionDescriptor


class StateObservation(BaseModel):
    """A session's lifecycle state changed."""

    native_id: str
    state: SessionState
    reason: str = ""
    at: datetime | None = None


class OutputObservation(BaseModel):
    """The agent (or its user) appended output to the session."""

    native_id: str
    role: str = "assistant"  # assistant | user | system
    text: str
    at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnPhase(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"


class TurnObservation(BaseModel):
    """An agent turn began or ended."""

    native_id: str
    phase: TurnPhase
    at: datetime | None = None


class ToolPhase(StrEnum):
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"


class ToolObservation(BaseModel):
    """The agent used a tool."""

    native_id: str
    phase: ToolPhase
    tool: str
    tool_use_id: str = ""
    detail: str = ""
    at: datetime | None = None


class InteractionObservation(BaseModel):
    """The agent is blocked on a human (permission request or question).

    The manager verifies the adapter declares the matching respond capability,
    opens an interaction with the Mediation Service, and moves the session to
    ``waiting_on_user``. The answer comes back through ``adapter.respond()``.
    """

    native_id: str
    interaction_native_id: str  # e.g. a tool_use_id; unique within the adapter
    kind: InteractionKind
    title: str
    body: str = ""
    options: list[str] = Field(default_factory=list)
    #: Seconds until auto-resolution; None uses the server default.
    timeout_s: float | None = None
    at: datetime | None = None


class InteractionClosedObservation(BaseModel):
    """The agent stopped waiting without an answer from us (e.g. the user
    answered in the terminal); the pending interaction should be cancelled."""

    native_id: str
    interaction_native_id: str
    reason: str = ""
    at: datetime | None = None


Observation = (
    SessionObservation
    | StateObservation
    | OutputObservation
    | TurnObservation
    | ToolObservation
    | InteractionObservation
    | InteractionClosedObservation
)
