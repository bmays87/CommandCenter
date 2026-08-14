"""The FastAPI application: REST queries, commands, + the WebSocket stream.

Queries are a thin view over the Session Registry, Mediation Service, and the
event log. Commands (answering interactions, launching/terminating sessions)
flow inward to the injected services and may be rejected; the resulting facts
flow outward on the bus. The dashboard is served from ``dashboard_dir`` when
it exists, so a single process serves both API and UI.
"""

import asyncio
import contextlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from ulid import ULID

from prodeo.adapters import AdapterInfo, AdapterManager, LaunchSpec
from prodeo.api.auth import make_auth_dependency, websocket_authorized
from prodeo.apps import AppStatus, AppSupervisor
from prodeo.bus.interface import BackpressurePolicy, EventBus, matches
from prodeo.environment import EnvironmentReport
from prodeo.environment import report as environment_report
from prodeo.errors import (
    AdapterOperationError,
    AppNotReadyError,
    CapabilityNotSupportedError,
    IllegalTransitionError,
    InteractionAlreadyResolvedError,
    InvalidScheduleError,
    MachineActionError,
    NotEntitledError,
    ProdeoError,
    RequirementsNotMetError,
    UnknownAdapterError,
    UnknownAppError,
    UnknownExtensionError,
    UnknownInteractionError,
    UnknownScheduleError,
    UnknownSessionError,
)
from prodeo.events import Event, new_event
from prodeo.extensions import (
    AssetProvisioner,
    AssetResult,
    AssetStatus,
    Catalog,
    ExtensionCatalog,
    ExtensionConfig,
    ExtensionDetail,
    ExtensionService,
    ExtensionSettings,
    ExtensionSummary,
    InstallResult,
)
from prodeo.machine import MachineActions
from prodeo.mediation import (
    Answer,
    Interaction,
    InteractionKind,
    InteractionStatus,
    MediationService,
    QuestionGroup,
)
from prodeo.persistence.interface import EventQuery, EventStore
from prodeo.presence import ClientPresence, PresenceTracker
from prodeo.scheduler import Schedule, SchedulerService
from prodeo.sessions import Session, SessionRegistry, SessionState

_log = structlog.get_logger(__name__)

MAX_EVENT_LIMIT = 1000

#: Domain errors -> HTTP status. Anything else under ProdeoError is a 500.
_ERROR_STATUS: dict[type[ProdeoError], int] = {
    UnknownSessionError: 404,
    UnknownInteractionError: 404,
    UnknownScheduleError: 404,
    UnknownExtensionError: 404,
    UnknownAppError: 404,
    UnknownAdapterError: 400,
    CapabilityNotSupportedError: 400,
    InvalidScheduleError: 400,
    InteractionAlreadyResolvedError: 409,
    #: Conflict - the session is not in a state this transition allows (e.g.
    #: archiving one that is still running; stop it first).
    IllegalTransitionError: 409,
    #: Payment Required - the extension exists, the caller may not have it.
    NotEntitledError: 402,
    #: Precondition Failed - this machine cannot run it (no GPU, wrong Python).
    RequirementsNotMetError: 412,
    #: Precondition Failed - setup steps incomplete; the detail lists them.
    AppNotReadyError: 412,
    #: The machine cannot perform the action (no editor, bad path); the
    #: message says what to fix.
    MachineActionError: 400,
    AdapterOperationError: 502,
}


#: Identifies this process. Changes on every boot, which is what lets a client
#: tell "the server I asked to restart is back" from "it has not gone down yet"
#: - the two are indistinguishable from a plain reachability check.
BOOT_ID = str(ULID())


class HealthResponse(BaseModel):
    status: str
    version: str
    node: str
    #: New on every process start; see :data:`BOOT_ID`.
    boot_id: str = BOOT_ID


class SessionListResponse(BaseModel):
    sessions: list[Session]


class EventListResponse(BaseModel):
    events: list[Event]
    #: Pass as ``after`` (REST) or ``?after=`` (WebSocket) to resume the stream.
    cursor: str | None


class InteractionListResponse(BaseModel):
    interactions: list[Interaction]
    pending: int


class AnswerRequest(BaseModel):
    """A human's answer: ``decision`` for permissions, ``text`` or
    ``selections`` for questions (``text`` doubles as the deny reason)."""

    decision: Literal["allow", "deny"] | None = None
    text: str = ""
    updated_input: dict[str, Any] | None = None
    #: Structured answer to a question-kind interaction: group id -> labels.
    selections: dict[str, list[str]] | None = None


class ExternalInteractionRequest(BaseModel):
    """An externally blocked requester (e.g. a permission hook, ADR-0011)
    submitting an interaction and long-polling for its resolution."""

    adapter: str
    session_native_id: str
    kind: InteractionKind = InteractionKind.PERMISSION
    title: str
    body: str = ""
    options: list[str] = Field(default_factory=list)
    questions: list[QuestionGroup] = Field(default_factory=list)
    #: Adapter-native interaction id; the server assigns a ULID when empty.
    native_id: str = ""
    timeout_s: float = Field(gt=0, le=3600)


class ExternalInteractionResponse(BaseModel):
    """The interaction's terminal state; ``answer`` only when answered (the
    requester falls through to its own prompt on timeout/cancellation)."""

    interaction_id: str
    status: InteractionStatus
    answer: Answer | None = None


class LaunchRequest(BaseModel):
    adapter: str
    project: str = ""
    prompt: str = ""
    model: str = ""
    permission_mode: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class PromptRequest(BaseModel):
    prompt: str


class SetModelRequest(BaseModel):
    """Switch an active session's model (empty = the agent's default)."""

    model: str = ""


#: Adapter-native permission modes we accept, with the human labels the
#: dashboard shows. Validated here so an unknown mode never reaches an adapter.
PERMISSION_MODES: dict[str, str] = {
    "default": "Manual",
    "plan": "Plan",
    "acceptEdits": "Edit Automatically",
    "bypassPermissions": "Auto",
}


class SetPermissionModeRequest(BaseModel):
    """Switch how an active session handles permissions."""

    mode: Literal["default", "plan", "acceptEdits", "bypassPermissions"]


class ContextUsage(BaseModel):
    """Context-window usage for a live session, mapped from the adapter's
    (CLI-owned) shape into a stable, small view for the composer pill."""

    #: Percent of the context window in use (0-100).
    percentage: float = 0.0
    total_tokens: int = 0
    max_tokens: int = 0
    #: The model the usage was computed for; may differ from a just-switched one.
    model: str = ""


class ScheduleListResponse(BaseModel):
    schedules: list[Schedule]


class PresenceReport(BaseModel):
    """One client heartbeat: 'I am here, and the user is/isn't engaged.'"""

    kind: str = "client"
    attentive: bool = False
    node: str = ""
    #: Seconds until this heartbeat expires; clients re-report well inside it.
    ttl_s: float = Field(30.0, gt=0, le=3600)


class PresenceListResponse(BaseModel):
    clients: list[ClientPresence]
    any_attentive: bool


class VoiceEventRequest(BaseModel):
    """A ``voice.*`` event reported by a voice client (e.g. Mjölnir).

    Only the ``voice.`` namespace may be ingested; everything else in the log
    is written by the core services and adapters themselves.
    """

    type: str
    client_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    correlation_id: str | None = None
    #: Originating machine (the satellite's node name); empty = this server's.
    node: str = ""


class CreateScheduleRequest(BaseModel):
    """Define a cron-style agent launch (the launch fields mirror LaunchRequest)."""

    name: str
    cron: str
    adapter: str
    project: str = ""
    prompt: str = ""
    model: str = ""
    permission_mode: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class AdapterListResponse(BaseModel):
    """Loaded adapters with their capabilities and declared model catalogs."""

    adapters: list[AdapterInfo]


class ExtensionListResponse(BaseModel):
    extensions: list[ExtensionSummary]


class ExtensionConfigRequest(BaseModel):
    """Replace one extension's saved config overlay."""

    values: dict[str, Any] = Field(default_factory=dict)


class ExtensionEnabledRequest(BaseModel):
    """Turn an installed extension on or off for the next boot."""

    enabled: bool


class AssetListResponse(BaseModel):
    assets: list[AssetStatus]


class AppListResponse(BaseModel):
    apps: list[AppStatus]


class AppAutostartRequest(BaseModel):
    """Whether an app launches with the server."""

    autostart: bool


class RestartResponse(BaseModel):
    """Acknowledges a restart request; the process is still up when it is sent."""

    restarting: bool
    #: The boot_id being replaced. Poll ``/api/health`` until it differs.
    boot_id: str


class OpenEditorRequest(BaseModel):
    """A project directory to open in the code editor, in a new window."""

    path: str


class OpenEditorResponse(BaseModel):
    opened: bool
    path: str


class DirectoryEntry(BaseModel):
    name: str
    path: str


class DirectoryListing(BaseModel):
    """One directory's sub-directories, for the path picker."""

    #: Empty at the root listing, which has no single path (Windows has drives).
    path: str = ""
    #: ``None`` at a root, so the UI knows not to offer "up".
    parent: str | None = None
    entries: list[DirectoryEntry] = Field(default_factory=list)


def _root_entries() -> list[DirectoryEntry]:
    """Where a browse with no path starts: drives on Windows, ``/`` elsewhere.

    Home is offered on both, because it is where a user most often wants to
    put things and is otherwise several clicks deep.
    """
    roots: list[DirectoryEntry] = []
    listdrives = getattr(os, "listdrives", None)
    if sys.platform == "win32" and listdrives is not None:
        with contextlib.suppress(OSError):
            for drive in listdrives():
                # listdrives() reports drives that are not ready - an empty
                # optical drive, a disconnected network mapping. Offering one
                # is offering a dead end.
                with contextlib.suppress(OSError):
                    if Path(drive).is_dir():
                        roots.append(DirectoryEntry(name=drive, path=drive))
    else:
        roots.append(DirectoryEntry(name="/", path="/"))
    with contextlib.suppress(OSError, RuntimeError):
        home = Path.home()
        if home.is_dir():
            roots.append(DirectoryEntry(name=f"Home ({home.name})", path=str(home)))
    return roots


def _list_directories(path: str) -> DirectoryListing:
    """Sub-directories of ``path``. Blocking; callers use ``to_thread``."""
    if not path:
        return DirectoryListing(entries=_root_entries())
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"bad path: {exc}") from exc
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"{path!r} is not a directory")
    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.casefold())
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"cannot list {target}: {exc}") from exc
    entries: list[DirectoryEntry] = []
    for child in children:
        # One unreadable child - C:\System Volume Information, a dead symlink -
        # must not fail the listing the user is actually looking at.
        with contextlib.suppress(OSError):
            if child.is_dir():
                entries.append(DirectoryEntry(name=child.name, path=str(child)))
    parent = target.parent
    return DirectoryListing(
        path=str(target),
        # At a filesystem root a path is its own parent; that is the signal.
        parent=None if parent == target else str(parent),
        entries=entries,
    )


def create_app(
    *,
    registry: SessionRegistry,
    store: EventStore,
    bus: EventBus,
    mediation: MediationService,
    manager: AdapterManager,
    scheduler: SchedulerService,
    presence: PresenceTracker,
    node: str,
    version: str,
    extensions: ExtensionService | None = None,
    catalog: ExtensionCatalog | None = None,
    apps: AppSupervisor | None = None,
    assets: AssetProvisioner | None = None,
    api_token: str | None = None,
    dashboard_dir: Path | None = None,
    #: Injected by the composition root; records the intent and releases the
    #: idle wait in ``prodeo.server.run``. None = restart is not offered.
    restart_fn: Callable[[], None] | None = None,
    #: Machine-local actions (ADR-0020); the seam CCAN will occupy. None =
    #: those endpoints answer 503.
    machine: MachineActions | None = None,
) -> FastAPI:
    app = FastAPI(title="Prodeo Command Center", version=version)
    auth = Depends(make_auth_dependency(api_token))

    @app.exception_handler(ProdeoError)
    async def domain_error(_request: Request, exc: ProdeoError) -> JSONResponse:
        status = _ERROR_STATUS.get(type(exc), 500)
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:  # health is unauthenticated by design
        return HealthResponse(status="ok", version=version, node=node, boot_id=BOOT_ID)

    @app.get("/api/sessions", response_model=SessionListResponse, dependencies=[auth])
    async def list_sessions() -> SessionListResponse:
        return SessionListResponse(sessions=registry.list_sessions())

    @app.get("/api/sessions/{session_id}", response_model=Session, dependencies=[auth])
    async def get_session(session_id: str) -> Session:
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    @app.get("/api/events", response_model=EventListResponse, dependencies=[auth])
    async def list_events(
        after: str | None = None,
        before: str | None = Query(None, description="exclusive cursor for paging backward"),
        type: str = Query("*", description="event type pattern: exact, `ns.*`, or `*`"),
        session: str | None = None,
        limit: int = Query(500, ge=1, le=MAX_EVENT_LIMIT),
        order: Literal["asc", "desc"] = Query("asc", description="`desc` = newest first"),
    ) -> EventListResponse:
        events = await store.query(
            EventQuery(
                after_id=after,
                before_id=before,
                type_pattern=type,
                session_id=session,
                limit=limit,
                order=order,
            )
        )
        # The cursor continues the walk in the requested direction: pass it as
        # `after` (asc) or `before` (desc) on the next request.
        fallback = after if order == "asc" else before
        return EventListResponse(events=events, cursor=events[-1].id if events else fallback)

    @app.get(
        "/api/sessions/{session_id}/events",
        response_model=EventListResponse,
        dependencies=[auth],
    )
    async def session_events(
        session_id: str,
        after: str | None = None,
        type: str = Query("*"),
        limit: int = Query(500, ge=1, le=MAX_EVENT_LIMIT),
    ) -> EventListResponse:
        if registry.get(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        events = await store.query(
            EventQuery(after_id=after, type_pattern=type, session_id=session_id, limit=limit)
        )
        return EventListResponse(events=events, cursor=events[-1].id if events else after)

    @app.get(
        "/api/interactions",
        response_model=InteractionListResponse,
        dependencies=[auth],
    )
    async def list_interactions(
        status: InteractionStatus | None = None,
        session: str | None = None,
    ) -> InteractionListResponse:
        return InteractionListResponse(
            interactions=mediation.list_interactions(status=status, session_id=session),
            pending=mediation.pending_count(),
        )

    @app.post(
        "/api/interactions/{interaction_id}/answer",
        response_model=Interaction,
        dependencies=[auth],
    )
    async def answer_interaction(interaction_id: str, body: AnswerRequest) -> Interaction:
        """Resolve an interaction; the first answer wins (409 afterwards)."""
        answer = Answer(
            decision=body.decision,
            text=body.text,
            updated_input=body.updated_input,
            selections=body.selections,
        )
        return await mediation.answer(interaction_id, answer, answered_by="api")

    @app.post(
        "/api/interactions/external",
        response_model=ExternalInteractionResponse,
        dependencies=[auth],
    )
    async def external_interaction(
        request: Request, body: ExternalInteractionRequest
    ) -> ExternalInteractionResponse:
        """Open an interaction for an externally blocked agent and long-poll.

        The response is only sent once the interaction leaves ``pending``
        (answered, timed out, or cancelled). If the requester disconnects
        first — its human answered locally — the interaction is withdrawn.
        """
        interaction, resolved = await manager.open_external_interaction(
            adapter=body.adapter,
            session_native_id=body.session_native_id,
            native_id=body.native_id or str(ULID()),
            kind=body.kind,
            title=body.title,
            body=body.body,
            options=body.options,
            questions=body.questions,
            timeout_s=body.timeout_s,
        )
        try:
            while True:
                await asyncio.wait({resolved}, timeout=0.5)
                current = mediation.get(interaction.id)
                if current is not None and current.status is not InteractionStatus.PENDING:
                    return ExternalInteractionResponse(
                        interaction_id=current.id,
                        status=current.status,
                        answer=(
                            current.answer if current.status is InteractionStatus.ANSWERED else None
                        ),
                    )
                if await request.is_disconnected():
                    await manager.withdraw_external_interaction(
                        interaction.id, reason="requester_disconnected"
                    )
                    return ExternalInteractionResponse(
                        interaction_id=interaction.id,
                        status=InteractionStatus.CANCELLED,
                    )
        except asyncio.CancelledError:
            await manager.withdraw_external_interaction(
                interaction.id, reason="requester_disconnected"
            )
            raise

    @app.post("/api/sessions", response_model=Session, status_code=201, dependencies=[auth])
    async def launch_session(body: LaunchRequest) -> Session:
        """Launch a new agent run through a control-capable adapter."""
        spec = LaunchSpec(
            project=body.project,
            prompt=body.prompt,
            model=body.model,
            permission_mode=body.permission_mode,
            options=body.options,
        )
        return await manager.launch(body.adapter, spec)

    @app.post(
        "/api/sessions/{session_id}/terminate",
        response_model=Session,
        dependencies=[auth],
    )
    async def terminate_session(session_id: str) -> Session:
        await manager.terminate(session_id)
        session = registry.get(session_id)
        if session is None:  # pragma: no cover - terminate already 404s first
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    @app.post(
        "/api/sessions/{session_id}/prompt",
        response_model=Session,
        dependencies=[auth],
    )
    async def prompt_session(session_id: str, body: PromptRequest) -> Session:
        await manager.send_prompt(session_id, body.prompt)
        session = registry.get(session_id)
        if session is None:  # pragma: no cover - send_prompt already 404s first
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    @app.post(
        "/api/sessions/{session_id}/model",
        response_model=Session,
        dependencies=[auth],
    )
    async def set_session_model(session_id: str, body: SetModelRequest) -> Session:
        """Switch the model a controlled session runs on, mid-session."""
        await manager.set_model(session_id, body.model)
        session = registry.get(session_id)
        if session is None:  # pragma: no cover - set_model already 404s first
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    @app.post(
        "/api/sessions/{session_id}/permission-mode",
        response_model=Session,
        dependencies=[auth],
    )
    async def set_session_permission_mode(
        session_id: str, body: SetPermissionModeRequest
    ) -> Session:
        """Switch how a controlled session handles permissions, mid-session.

        Plan (no execution), Manual (ask each time), Edit Automatically
        (auto-accept edits), or Auto (bypass checks). Applies to the *running*
        agent; a launch sets the starting mode via ``LaunchRequest``.
        """
        await manager.set_permission_mode(session_id, body.mode)
        session = registry.get(session_id)
        if session is None:  # pragma: no cover - set_permission_mode 404s first
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    @app.post(
        "/api/sessions/{session_id}/interrupt",
        response_model=Session,
        dependencies=[auth],
    )
    async def interrupt_session(session_id: str) -> Session:
        """Stop the session's current turn without ending it.

        Unlike ``/terminate``, the session stays alive and keeps any queued
        follow-up prompt — this is the composer's Stop button.
        """
        await manager.interrupt(session_id)
        session = registry.get(session_id)
        if session is None:  # pragma: no cover - interrupt already 404s first
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    @app.get(
        "/api/sessions/{session_id}/context",
        response_model=ContextUsage,
        dependencies=[auth],
    )
    async def session_context(session_id: str) -> ContextUsage:
        """Context-window usage for a live session (the composer pill).

        The adapter's shape is CLI-owned, so map it defensively: unknown or
        missing fields become zeros rather than an error, and the pill hides
        itself when nothing useful comes back.
        """
        raw = await manager.context_usage(session_id)
        return ContextUsage(
            percentage=float(raw.get("percentage", 0) or 0),
            total_tokens=int(raw.get("totalTokens", 0) or 0),
            max_tokens=int(raw.get("maxTokens", 0) or 0),
            model=str(raw.get("model", "") or ""),
        )

    @app.post(
        "/api/sessions/{session_id}/archive",
        response_model=Session,
        dependencies=[auth],
    )
    async def archive_session(session_id: str) -> Session:
        """Remove a finished session from the fleet (event-sourced, reversible).

        Archiving is a state transition, not a hard delete: the log is the
        durable record (ADR-0002), so the session leaves the active view but
        its history is intact. Only terminal sessions can be archived — a
        running one must be stopped first (409).
        """
        _require_write()
        return await registry.observe_state(
            session_id, SessionState.ARCHIVED, reason="archived_by_user"
        )

    @app.get("/api/schedules", response_model=ScheduleListResponse, dependencies=[auth])
    async def list_schedules() -> ScheduleListResponse:
        return ScheduleListResponse(schedules=scheduler.list_schedules())

    @app.post("/api/schedules", response_model=Schedule, status_code=201, dependencies=[auth])
    async def create_schedule(body: CreateScheduleRequest) -> Schedule:
        """Define a cron-style agent launch (400 for an invalid expression)."""
        return await scheduler.create(
            name=body.name,
            cron=body.cron,
            adapter=body.adapter,
            spec=LaunchSpec(
                project=body.project,
                prompt=body.prompt,
                model=body.model,
                permission_mode=body.permission_mode,
                options=body.options,
            ),
        )

    @app.delete("/api/schedules/{schedule_id}", status_code=204, dependencies=[auth])
    async def delete_schedule(schedule_id: str) -> None:
        await scheduler.delete(schedule_id)

    @app.post(
        "/api/schedules/{schedule_id}/trigger",
        response_model=Schedule,
        dependencies=[auth],
    )
    async def trigger_schedule(schedule_id: str) -> Schedule:
        """Fire a schedule immediately, without waiting for its cron slot."""
        return await scheduler.trigger_now(schedule_id)

    @app.get("/api/presence", response_model=PresenceListResponse, dependencies=[auth])
    async def list_presence() -> PresenceListResponse:
        """Live clients and whether any of them holds the user's attention."""
        return PresenceListResponse(
            clients=presence.list_clients(),
            any_attentive=presence.any_attentive(),
        )

    @app.put("/api/presence/{client_id}", response_model=ClientPresence, dependencies=[auth])
    async def report_presence(client_id: str, body: PresenceReport) -> ClientPresence:
        """Heartbeat: entries expire after ``ttl_s`` unless re-reported."""
        return presence.report(
            client_id,
            kind=body.kind,
            attentive=body.attentive,
            ttl_s=body.ttl_s,
            node=body.node,
        )

    @app.delete("/api/presence/{client_id}", status_code=204, dependencies=[auth])
    async def forget_presence(client_id: str) -> None:
        """Clean goodbye; missing clients are fine (expiry races are expected)."""
        presence.forget(client_id)

    @app.post("/api/voice/events", response_model=Event, status_code=201, dependencies=[auth])
    async def ingest_voice_event(body: VoiceEventRequest) -> Event:
        """Ingest one ``voice.*`` event from a voice client into the log."""
        if not body.type.startswith("voice.") or body.type == "voice.":
            raise HTTPException(status_code=400, detail="only voice.* events may be ingested")
        event = new_event(
            body.type,
            node=body.node or node,
            source=f"voice:{body.client_id}",
            session_id=body.session_id,
            correlation_id=body.correlation_id,
            payload=body.payload,
        )
        await bus.publish(event)
        return event

    @app.get("/api/adapters", response_model=AdapterListResponse, dependencies=[auth])
    async def list_adapters() -> AdapterListResponse:
        """Started adapters with capabilities and declared model catalogs."""
        return AdapterListResponse(adapters=manager.describe_adapters())

    def _extensions() -> ExtensionService:
        if extensions is None:  # not wired (schema export, focused tests)
            raise HTTPException(status_code=503, detail="extensions manager not configured")
        return extensions

    @app.get("/api/extensions", response_model=ExtensionListResponse, dependencies=[auth])
    async def list_extensions() -> ExtensionListResponse:
        """Everything the Plugin Host discovered, including what it did not host."""
        return ExtensionListResponse(extensions=_extensions().list())

    @app.get("/api/extensions/catalog", response_model=Catalog, dependencies=[auth])
    async def extension_catalog() -> Catalog:
        """The sanctioned index, each entry annotated for this machine.

        Entries carry ``unmet``: the requirements this host fails, so the UI
        can say "needs an NVIDIA GPU" instead of letting a user discover it
        after a multi-gigabyte download.
        """
        return await _extensions().catalog()

    @app.get("/api/extensions/{name}", response_model=ExtensionDetail, dependencies=[auth])
    async def get_extension(name: str) -> ExtensionDetail:
        return _extensions().get(name)

    @app.get("/api/extensions/{name}/config", response_model=ExtensionConfig, dependencies=[auth])
    async def get_extension_config(name: str) -> ExtensionConfig:
        return await _extensions().config(name)

    def _require_write() -> None:
        """Guard every endpoint that changes machine state.

        Config values become plugin constructor arguments and installs execute
        code, both materially larger than the rest of this read-mostly API - and
        the API is open by default when no token is set (ADR-0015).
        """
        if api_token is None:
            raise HTTPException(
                status_code=403,
                detail="set PRODEO_API_TOKEN to change extensions",
            )

    @app.post("/api/extensions/{name}/install", response_model=InstallResult, dependencies=[auth])
    async def install_extension(name: str) -> InstallResult:
        """Install a catalog extension.

        The name is resolved against the sanctioned catalog and the package
        spec comes from there, so this cannot install arbitrary code; an
        unknown name is a 404.
        """
        _require_write()
        return await _extensions().install(name)

    @app.delete("/api/extensions/{name}/install", response_model=InstallResult, dependencies=[auth])
    async def uninstall_extension(name: str) -> InstallResult:
        _require_write()
        return await _extensions().uninstall(name)

    @app.put("/api/extensions/{name}/enabled", response_model=ExtensionSummary, dependencies=[auth])
    async def set_extension_enabled(name: str, body: ExtensionEnabledRequest) -> ExtensionSummary:
        """Turn an installed extension on or off for the next boot."""
        _require_write()
        return await _extensions().set_enabled(name, body.enabled)

    @app.get("/api/extension-settings", response_model=ExtensionSettings, dependencies=[auth])
    async def get_extension_settings() -> ExtensionSettings:
        return await _extensions().settings()

    @app.put("/api/extension-settings", response_model=ExtensionSettings, dependencies=[auth])
    async def put_extension_settings(body: ExtensionSettings) -> ExtensionSettings:
        """Manager-wide settings - notably where bulk model data is written."""
        _require_write()
        return await _extensions().set_settings(body)

    @app.get("/api/extensions/{name}/assets", response_model=AssetListResponse, dependencies=[auth])
    async def list_extension_assets(name: str) -> AssetListResponse:
        """Model files this extension needs, and whether the machine has them."""
        if assets is None:
            raise HTTPException(status_code=503, detail="asset provisioner not configured")
        return AssetListResponse(assets=await assets.list_assets(name))

    @app.post(
        "/api/extensions/{name}/assets/{asset_id}",
        response_model=AssetResult,
        dependencies=[auth],
    )
    async def download_extension_asset(name: str, asset_id: str) -> AssetResult:
        """Fetch one asset and wire its path into the owning app's config."""
        _require_write()
        if assets is None:
            raise HTTPException(status_code=503, detail="asset provisioner not configured")
        return await assets.download(name, asset_id)

    @app.put("/api/extensions/{name}/config", response_model=ExtensionConfig, dependencies=[auth])
    async def put_extension_config(name: str, body: ExtensionConfigRequest) -> ExtensionConfig:
        """Persist an extension's config overlay; applies on the next restart.

        Refused when the API has no token: these values become plugin
        constructor arguments, which is a materially larger blast radius than
        the rest of this read-mostly API, and an open server should not offer
        it (ADR-0014).
        """
        _require_write()
        try:
            return await _extensions().set_config(name, body.values)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc

    def _apps() -> AppSupervisor:
        if apps is None:  # not wired (schema export, focused tests)
            raise HTTPException(status_code=503, detail="app supervisor not configured")
        return apps

    @app.get("/api/system/environment", response_model=EnvironmentReport, dependencies=[auth])
    async def system_environment() -> EnvironmentReport:
        """What this machine is missing, and what would fix it.

        Read-only and cheap: hardware detection and a couple of local probes,
        so it is safe to poll from a settings page.
        """
        models_dir = ""
        if extensions is not None:
            models_dir = (await extensions.settings()).models_dir
        return await environment_report(models_dir)

    @app.post("/api/system/restart", response_model=RestartResponse, dependencies=[auth])
    async def restart_server(background: BackgroundTasks) -> RestartResponse:
        """Stop the server and start it again.

        Installing, enabling, or disabling an extension only takes effect at the
        next boot - the Plugin Host and the app supervisor both discover entry
        points once, at start. Without this the manager can install something
        and then only ask the user to go and restart it by hand, which is not a
        dashboard doing its job (ADR-0016).

        Refused when the API has no token: restarting a process is at least as
        consequential as the config writes that already require one.

        The shutdown runs as a background task so this response is flushed
        first; otherwise the client sees a dropped connection and cannot tell
        success from failure.
        """
        _require_write()
        if restart_fn is None:  # not wired (schema export, focused tests)
            raise HTTPException(status_code=503, detail="restart is not available on this server")
        background.add_task(restart_fn)
        return RestartResponse(restarting=True, boot_id=BOOT_ID)

    @app.post("/api/system/open-editor", response_model=OpenEditorResponse, dependencies=[auth])
    async def open_editor(body: OpenEditorRequest) -> OpenEditorResponse:
        """Open a project directory in VS Code, in a new window.

        Goes through the ``MachineActions`` seam (ADR-0020): today the server
        and the agent machine are the same host, so the local implementation
        runs ``code --new-window`` here; when CCAN splits out, the same call
        routes to the node that owns the project. One window per project is
        deliberate - several projects, several agents, side by side.

        Refused when the API has no token: it launches a program.
        """
        _require_write()
        if machine is None:  # not wired (schema export, focused tests)
            raise HTTPException(
                status_code=503, detail="machine actions are not available on this server"
            )
        await machine.open_editor(body.path)
        return OpenEditorResponse(opened=True, path=body.path)

    @app.get("/api/system/browse", response_model=DirectoryListing, dependencies=[auth])
    async def browse_directories(path: str = Query(default="")) -> DirectoryListing:
        """List sub-directories of ``path``, or the roots when it is empty.

        The picker has to run here rather than in the browser: the value being
        chosen is a path on *this* machine, and neither ``webkitdirectory`` nor
        ``showDirectoryPicker`` yields one. Listing server-side also makes the
        same UI work on Windows and POSIX for free.

        Refused when the API has no token: enumerating the filesystem is a
        larger disclosure than the rest of this read-mostly API, and the API is
        open by default when no token is set (ADR-0016).
        """
        _require_write()
        return await asyncio.to_thread(_list_directories, path)

    async def _with_setup(status: AppStatus) -> AppStatus:
        """Annotate a status with its unmet setup steps (ADR-0017).

        Computed at query time rather than stored: the answer changes when a
        wizard step completes, and the dashboard polls this anyway.
        """
        return status.model_copy(update={"unmet_setup": await _apps().unmet_setup(status.name)})

    @app.get("/api/apps", response_model=AppListResponse, dependencies=[auth])
    async def list_apps() -> AppListResponse:
        """Installed app extensions, whether each is running and is startable."""
        return AppListResponse(apps=[await _with_setup(s) for s in _apps().list()])

    @app.post("/api/apps/{name}/start", response_model=AppStatus, dependencies=[auth])
    async def start_app(name: str) -> AppStatus:
        """Start an app. 412 with the missing steps when setup is incomplete."""
        _require_write()
        return await _with_setup(await _apps().start_app(name))

    @app.post("/api/apps/{name}/stop", response_model=AppStatus, dependencies=[auth])
    async def stop_app(name: str) -> AppStatus:
        _require_write()
        return await _with_setup(await _apps().stop_app(name))

    @app.post("/api/apps/{name}/restart", response_model=AppStatus, dependencies=[auth])
    async def restart_app(name: str) -> AppStatus:
        _require_write()
        return await _with_setup(await _apps().restart_app(name))

    @app.put("/api/apps/{name}/autostart", response_model=AppStatus, dependencies=[auth])
    async def set_app_autostart(name: str, body: AppAutostartRequest) -> AppStatus:
        """Launch this app when the server starts. Off by default.

        A microphone-listening process should not start itself uninvited, so
        this is opt-in rather than a default the user has to discover.
        """
        _require_write()
        supervisor = _apps()
        supervisor.set_autostart(name, body.autostart)
        if extensions is not None:
            settings = await extensions.settings()
            chosen = set(settings.autostart)
            chosen.add(name) if body.autostart else chosen.discard(name)
            settings.autostart = sorted(chosen)
            await extensions.set_settings(settings)
        return await _with_setup(supervisor.status_of(name))

    @app.websocket("/api/ws/events")
    async def event_stream(ws: WebSocket) -> None:
        """Live event stream with ULID-cursor catch-up.

        Query params: ``after`` (last seen event id), ``types`` (comma-separated
        patterns, default ``*``), ``token``. Frames are event JSON. Delivery is
        best-effort (DROP_OLDEST); clients reconcile via ``GET /api/events``.
        """
        if not websocket_authorized(ws, api_token):
            await ws.close(code=4401, reason="invalid or missing API token")
            return
        await ws.accept()
        patterns = [p.strip() for p in ws.query_params.get("types", "*").split(",") if p.strip()]
        cursor = ws.query_params.get("after")

        def wanted(event: Event) -> bool:
            return any(matches(p, event.type) for p in patterns)

        # Subscribe before replaying so nothing falls in the gap; the ULID
        # cursor deduplicates the overlap.
        sub = bus.subscribe("*", name="ws-client", policy=BackpressurePolicy.DROP_OLDEST)

        async def pump() -> None:
            last = cursor or ""
            if cursor is not None:
                while True:
                    batch = await store.query(EventQuery(after_id=last or None, limit=500))
                    if not batch:
                        break
                    for event in batch:
                        if wanted(event):
                            await ws.send_text(event.model_dump_json())
                    last = batch[-1].id
            async for event in sub:
                if event.id > last and wanted(event):
                    await ws.send_text(event.model_dump_json())

        # The pump never reads from the socket, so a separate receive loop is
        # needed to notice disconnects (and let uvicorn shut down cleanly).
        pump_task = asyncio.create_task(pump(), name="ws-pump")
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await pump_task
            await sub.close()

    if dashboard_dir is not None and (dashboard_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dashboard_dir, html=True), name="dashboard")
        _log.info("api.dashboard_mounted", path=str(dashboard_dir))

    return app


class ApiServer:
    """Runs uvicorn inside the existing event loop (owned by server.py)."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        import uvicorn

        config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False)
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(), name="api-server")
        while not self._server.started and not self._task.done():
            await asyncio.sleep(0.01)
        if self._task.done():  # startup failed (e.g. port in use): surface it
            self._task.result()

    @property
    def port(self) -> int:
        """The bound port (useful when configured with port 0)."""
        servers = getattr(self._server, "servers", [])
        for s in servers:
            for sock in s.sockets:
                port: int = sock.getsockname()[1]
                return port
        raise RuntimeError("server is not listening")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
