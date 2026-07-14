"""The FastAPI application: REST queries + the WebSocket event stream.

The API is a thin, read-only view over the Session Registry and the event
log in Phase 1. Commands (answering interactions, launching sessions) arrive
in Phase 2. The dashboard is served from ``dashboard_dir`` when it exists, so
a single process serves both API and UI.
"""

import asyncio
import contextlib
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from prodeo.api.auth import make_auth_dependency, websocket_authorized
from prodeo.bus.interface import BackpressurePolicy, EventBus, matches
from prodeo.events import Event
from prodeo.persistence.interface import EventQuery, EventStore
from prodeo.sessions import Session, SessionRegistry

_log = structlog.get_logger(__name__)

MAX_EVENT_LIMIT = 1000


class HealthResponse(BaseModel):
    status: str
    version: str
    node: str


class SessionListResponse(BaseModel):
    sessions: list[Session]


class EventListResponse(BaseModel):
    events: list[Event]
    #: Pass as ``after`` (REST) or ``?after=`` (WebSocket) to resume the stream.
    cursor: str | None


def create_app(
    *,
    registry: SessionRegistry,
    store: EventStore,
    bus: EventBus,
    node: str,
    version: str,
    api_token: str | None = None,
    dashboard_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Prodeo Command Center", version=version)
    auth = Depends(make_auth_dependency(api_token))

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:  # health is unauthenticated by design
        return HealthResponse(status="ok", version=version, node=node)

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
        type: str = Query("*", description="event type pattern: exact, `ns.*`, or `*`"),
        session: str | None = None,
        limit: int = Query(500, ge=1, le=MAX_EVENT_LIMIT),
    ) -> EventListResponse:
        events = await store.query(
            EventQuery(after_id=after, type_pattern=type, session_id=session, limit=limit)
        )
        return EventListResponse(events=events, cursor=events[-1].id if events else after)

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
