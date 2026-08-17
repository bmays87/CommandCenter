"""The CCAN's HTTP surface: pairing, health, and the node command plane.

Deliberately no authentication logic in the handlers: the TLS listener in
:mod:`prodeo_ccan.main` demands a client certificate and trusts exactly one
— the parent hub's (ADR-0025). Any request that reaches a handler has
already proven it comes from the parent, so this surface is the hub talking
to its own machine: mirroring the event log, launching and steering
sessions, resolving interactions (ADR-0026).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from prodeo.adapters import AdapterInfo, LaunchSpec
from prodeo.bus.interface import BackpressurePolicy
from prodeo.errors import (
    AdapterOperationError,
    CapabilityNotSupportedError,
    IllegalTransitionError,
    InteractionAlreadyResolvedError,
    ProdeoError,
    UnknownAdapterError,
    UnknownInteractionError,
    UnknownSessionError,
)
from prodeo.events import Event
from prodeo.identity import Identity
from prodeo.mediation import Answer, Interaction
from prodeo.persistence.interface import EventQuery
from prodeo.sessions import Session
from prodeo_ccan import __version__
from prodeo_ccan.config import CcanConfig
from prodeo_ccan.node import NodeHost

#: Domain errors -> HTTP status, mirrored back to the hub verbatim by its
#: gateway. Anything else under ProdeoError is a 500.
_ERROR_STATUS: dict[type[ProdeoError], int] = {
    UnknownSessionError: 404,
    UnknownInteractionError: 404,
    UnknownAdapterError: 400,
    CapabilityNotSupportedError: 400,
    IllegalTransitionError: 409,
    InteractionAlreadyResolvedError: 409,
    AdapterOperationError: 502,
}


class PairRequest(BaseModel):
    """The hub introducing itself; informational."""

    hub_node: str = ""


class PairResponse(BaseModel):
    """Mirrors ``prodeo.machines.pairing._PairResponse``; keep in sync."""

    node: str
    name: str = ""
    version: str = ""
    certificate_pem: str = ""
    enroll_token: str


class CcanHealth(BaseModel):
    status: str
    node: str
    version: str


class EventBatch(BaseModel):
    """One page of this node's event log, oldest first."""

    events: list[Event]
    cursor: str | None = None


class NodeLaunchRequest(BaseModel):
    """Mirrors the hub's LaunchRequest minus the machine (it is this one)."""

    adapter: str
    project: str = ""
    prompt: str = ""
    model: str = ""
    permission_mode: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class NodeAdapterList(BaseModel):
    adapters: list[AdapterInfo]


class NodeAnswerRequest(BaseModel):
    """An answer forwarded from the hub for one of this node's interactions."""

    decision: str | None = None
    text: str = ""
    updated_input: dict[str, Any] | None = None
    selections: dict[str, list[str]] | None = None
    answered_by: str = "api"


def create_app(config: CcanConfig, identity: Identity, host: NodeHost | None = None) -> FastAPI:
    """The node app; ``host`` adds the command plane (None = pairing only)."""

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if host is not None:
            await host.start()
        try:
            yield
        finally:
            if host is not None:
                await host.stop()

    app = FastAPI(title="Prodeo CCAN", version=__version__, lifespan=lifespan)

    @app.exception_handler(ProdeoError)
    async def domain_error(_request: Request, exc: ProdeoError) -> JSONResponse:
        status = _ERROR_STATUS.get(type(exc), 500)
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.post("/ccan/v1/pair", response_model=PairResponse)
    async def pair(_body: PairRequest) -> PairResponse:
        """Answer the parent's Add Machine handshake (ADR-0025)."""
        return PairResponse(
            node=config.node_name,
            name=config.node_name,
            version=__version__,
            certificate_pem=identity.certificate_pem,
            enroll_token=config.enroll_token,
        )

    @app.get("/ccan/v1/health", response_model=CcanHealth)
    async def health() -> CcanHealth:
        return CcanHealth(status="ok", node=config.node_name, version=__version__)

    if host is None:
        return app

    def _host() -> NodeHost:
        return host

    def _session(session_id: str) -> Session:
        session = _host().registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    @app.get("/ccan/v1/events", response_model=EventBatch)
    async def events(
        after: str | None = None,
        limit: int = Query(500, ge=1, le=1000),
        wait_s: float = Query(0.0, ge=0.0, le=30.0),
    ) -> EventBatch:
        """One page of this node's log; long-polls up to ``wait_s`` when empty.

        Subscribe-before-query so an event landing between the two is never
        missed; the small settle sleep lets the recorder persist a burst
        before the re-query, keeping the returned page in log order.
        """
        node_host = _host()
        query = EventQuery(after_id=after, limit=limit)
        batch = await node_host.store.query(query)
        if not batch and wait_s > 0:
            sub = node_host.bus.subscribe(
                "*", name="hub-sync-poll", policy=BackpressurePolicy.DROP_OLDEST
            )
            try:
                iterator = aiter(sub)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(wait_s):
                        await anext(iterator)
                        await asyncio.sleep(0.05)
            finally:
                await sub.close()
            batch = await node_host.store.query(query)
        return EventBatch(events=batch, cursor=batch[-1].id if batch else after)

    @app.get("/ccan/v1/adapters", response_model=NodeAdapterList)
    async def list_adapters() -> NodeAdapterList:
        return NodeAdapterList(adapters=_host().adapters.describe_adapters())

    @app.post("/ccan/v1/sessions", response_model=Session, status_code=201)
    async def launch(body: NodeLaunchRequest) -> Session:
        return await _host().adapters.launch(
            body.adapter,
            LaunchSpec(
                project=body.project,
                prompt=body.prompt,
                model=body.model,
                permission_mode=body.permission_mode,
                options=body.options,
            ),
        )

    @app.post("/ccan/v1/sessions/{session_id}/terminate", response_model=Session)
    async def terminate(session_id: str) -> Session:
        await _host().adapters.terminate(session_id)
        return _session(session_id)

    @app.post("/ccan/v1/sessions/{session_id}/prompt", response_model=Session)
    async def prompt(session_id: str, body: dict[str, Any]) -> Session:
        await _host().adapters.send_prompt(session_id, str(body.get("prompt", "")))
        return _session(session_id)

    @app.post("/ccan/v1/sessions/{session_id}/model", response_model=Session)
    async def set_model(session_id: str, body: dict[str, Any]) -> Session:
        await _host().adapters.set_model(session_id, str(body.get("model", "")))
        return _session(session_id)

    @app.post("/ccan/v1/sessions/{session_id}/permission-mode", response_model=Session)
    async def set_permission_mode(session_id: str, body: dict[str, Any]) -> Session:
        await _host().adapters.set_permission_mode(session_id, str(body.get("mode", "")))
        return _session(session_id)

    @app.post("/ccan/v1/sessions/{session_id}/interrupt", response_model=Session)
    async def interrupt(session_id: str) -> Session:
        await _host().adapters.interrupt(session_id)
        return _session(session_id)

    @app.get("/ccan/v1/sessions/{session_id}/context")
    async def context_usage(session_id: str) -> dict[str, Any]:
        return await _host().adapters.context_usage(session_id)

    @app.post("/ccan/v1/interactions/{interaction_id}/answer", response_model=Interaction)
    async def answer(interaction_id: str, body: NodeAnswerRequest) -> Interaction:
        """Resolve one of this node's interactions with the hub's answer."""
        decision = body.decision if body.decision in ("allow", "deny") else None
        return await _host().mediation.answer(
            interaction_id,
            Answer(
                decision=decision,
                text=body.text,
                updated_input=body.updated_input,
                selections=body.selections,
            ),
            answered_by=body.answered_by,
        )

    return app
