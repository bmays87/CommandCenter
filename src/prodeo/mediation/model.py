"""Interaction domain model.

Permission requests and agent questions share one mechanism deliberately:
both are "the agent is blocked on a human," differing only in answer type
(docs/architecture/event-model.md). These types are the contract shared by
the mediation service, the adapter boundary, and the API.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class InteractionKind(StrEnum):
    PERMISSION = "permission"
    QUESTION = "question"


class InteractionStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class QuestionOption(BaseModel):
    """One choice offered by a structured question."""

    label: str
    description: str = ""


class QuestionGroup(BaseModel):
    """One question in a (possibly multi-part) question-kind interaction.

    Agent-agnostic: "the agent asks one or more grouped multiple-choice
    questions". ``id`` is the stable key an answer's ``selections`` uses;
    only the adapter that opened the interaction knows how to map selections
    back to its agent's native input (ADR-0022).
    """

    id: str
    prompt: str
    options: list[QuestionOption]
    multi_select: bool = False


class Answer(BaseModel):
    """A human's resolution of an interaction.

    Permissions use ``decision`` (optionally with ``updated_input`` to run the
    tool with edited arguments); questions use ``text`` (a single label or
    free text) or ``selections`` (structured: group id -> chosen labels).
    For a denied permission, ``text`` carries the reason shown to the agent.
    """

    decision: Literal["allow", "deny"] | None = None
    text: str = ""
    updated_input: dict[str, Any] | None = None
    #: Structured answer to a question-kind interaction's ``questions``.
    selections: dict[str, list[str]] | None = None


class InteractionRequest(BaseModel):
    """What is submitted (via the Adapter Manager) to open an interaction."""

    session_id: str
    adapter: str
    native_id: str  # adapter-native interaction id (e.g. a tool_use_id)
    kind: InteractionKind
    title: str
    body: str = ""
    options: list[str] = Field(default_factory=list)
    #: Structured questions (question kind); empty for permissions and for
    #: adapters that only supply flat ``options``.
    questions: list[QuestionGroup] = Field(default_factory=list)
    #: Seconds until the interaction auto-resolves; None falls back to the
    #: service default (which may also be None = wait forever).
    timeout_s: float | None = None


class Interaction(BaseModel):
    """The canonical record of one interaction (mutable, like Session)."""

    id: str  # ULID, Command-Center-assigned
    session_id: str
    adapter: str
    native_id: str
    #: Node identity of the mediation service that opened it. What routes an
    #: answer to the owning machine, and what keeps the hub's restart sweep
    #: from cancelling remote pendings (ADR-0026). Additive: pre-Phase-6
    #: stored events rebuild as "local", which is what they were.
    node: str = "local"
    kind: InteractionKind
    title: str
    body: str = ""
    options: list[str] = Field(default_factory=list)
    questions: list[QuestionGroup] = Field(default_factory=list)
    requested_at: datetime
    timeout_at: datetime | None = None
    status: InteractionStatus = InteractionStatus.PENDING
    answered_by: str = ""
    answered_at: datetime | None = None
    answer: Answer | None = None
