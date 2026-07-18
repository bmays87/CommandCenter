"""AdapterManager: supervises adapters' tasks and translates observations
into domain events (discovery of adapter *plugins* is the Plugin Host's job).

Containment is the job here: an adapter exception must never take down the
manager or corrupt the event stream. Every call into adapter code is guarded;
failures become ``adapter.error`` events and (for watch tasks) supervised
restarts with exponential backoff.
"""

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import structlog

from prodeo.adapters.context import AdapterContext, ReportFn
from prodeo.adapters.interface import (
    AgentAdapter,
    InteractionRef,
    LaunchSpec,
    SessionRef,
)
from prodeo.adapters.observations import (
    InteractionClosedObservation,
    InteractionObservation,
    Observation,
    OutputObservation,
    SessionObservation,
    StateObservation,
    ToolObservation,
    ToolPhase,
    TurnObservation,
    TurnPhase,
)
from prodeo.bus.interface import EventBus
from prodeo.errors import (
    AdapterOperationError,
    CapabilityNotSupportedError,
    IllegalTransitionError,
    UnknownAdapterError,
    UnknownSessionError,
)
from prodeo.events import new_event
from prodeo.events import types as ev
from prodeo.mediation import (
    Answer,
    DeliverFn,
    Interaction,
    InteractionKind,
    InteractionRequest,
    InteractionStatus,
    MediationService,
)
from prodeo.sessions import Session, SessionDescriptor, SessionRegistry
from prodeo.sessions.state import END_STATES, SessionState

_log = structlog.get_logger(__name__)

#: Output payloads are capped so one giant transcript record cannot bloat the
#: event log; the full text always remains in the agent's own files.
MAX_OUTPUT_CHARS = 20_000

_TOOL_EVENT = {
    ToolPhase.STARTED: ev.TOOL_STARTED,
    ToolPhase.FINISHED: ev.TOOL_FINISHED,
    ToolPhase.FAILED: ev.TOOL_FAILED,
}


class AdapterManager:
    """Owns every adapter's lifecycle, discovery, and watch supervision."""

    def __init__(
        self,
        bus: EventBus,
        registry: SessionRegistry,
        mediation: MediationService,
        *,
        data_dir: Path,
        node: str = "local",
        adapter_config: dict[str, dict[str, Any]] | None = None,
        discovery_interval: float = 10.0,
    ) -> None:
        self._bus = bus
        self._registry = registry
        self._mediation = mediation
        self._data_dir = data_dir
        self._node = node
        self._adapter_config = adapter_config or {}
        self._discovery_interval = discovery_interval
        self._adapters: dict[str, AgentAdapter] = {}
        self._started: set[str] = set()
        self._watches: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._discovery_task: asyncio.Task[None] | None = None
        self._stopping = False

    # ------------------------------------------------------------- loading

    def add(self, adapter: AgentAdapter) -> None:
        """Register an adapter instance (the Plugin Host, tests, embedded use)."""
        self._adapters[adapter.metadata.name] = adapter

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        for name, adapter in self._adapters.items():
            ctx = AdapterContext(
                adapter_name=name,
                report=self._reporter(name),
                config=dict(self._adapter_config.get(name, {})),
                data_dir=self._data_dir / "adapters" / name,
            )
            ctx.data_dir.mkdir(parents=True, exist_ok=True)
            try:
                await adapter.start(ctx)
            except Exception as exc:
                _log.exception("adapter.start_failed", adapter=name)
                await self._error(name, "start_failed", str(exc))
                continue
            self._started.add(name)
            await self._bus.publish(
                new_event(
                    ev.ADAPTER_LOADED,
                    node=self._node,
                    source=f"adapter:{name}",
                    payload={
                        "name": name,
                        "version": adapter.metadata.version,
                        "capabilities": adapter.capabilities.model_dump(),
                    },
                )
            )
            await self._discover(name, adapter)
        if self._discovery_interval > 0:
            self._discovery_task = asyncio.create_task(
                self._discovery_loop(), name="adapter-discovery"
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._discovery_task is not None:
            self._discovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._discovery_task
        for task in list(self._watches.values()):
            task.cancel()
        if self._watches:
            await asyncio.gather(*self._watches.values(), return_exceptions=True)
        self._watches.clear()
        for name in list(self._started):
            adapter = self._adapters[name]
            try:
                await adapter.stop()
            except Exception as exc:
                _log.exception("adapter.stop_failed", adapter=name)
                await self._error(name, "stop_failed", str(exc))
            await self._bus.publish(
                new_event(
                    ev.ADAPTER_UNLOADED,
                    node=self._node,
                    source=f"adapter:{name}",
                    payload={"name": name},
                )
            )
        self._started.clear()

    # ------------------------------------------------------------- control

    async def launch(self, adapter_name: str, spec: LaunchSpec) -> Session:
        """Launch a new agent run, register its session, and start watching it."""
        adapter = self._require_adapter(adapter_name)
        self._require_capability("launch", adapter.capabilities.launch)
        try:
            ref = await adapter.launch(spec)
        except Exception as exc:
            raise await self._operation_failed(adapter_name, "launch", exc) from exc
        session = await self._registry.upsert_discovered(
            adapter_name,
            SessionDescriptor(
                native_id=ref.native_id,
                title=spec.prompt[:80],
                project=spec.project,
                model=spec.model,
                state=SessionState.STARTING,
                metadata={"controlled": "true"},
            ),
        )
        self._ensure_watch(adapter_name, adapter, ref.native_id, session.id, session.state)
        return session

    async def terminate(self, session_id: str) -> None:
        """Terminate a session through its owning adapter."""
        session, adapter = self._require_session(session_id)
        self._require_capability("terminate", adapter.capabilities.terminate)
        try:
            await adapter.terminate(self._session_ref(session))
        except Exception as exc:
            raise await self._operation_failed(
                session.adapter, "terminate", exc, session_id=session.id
            ) from exc

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        """Send a follow-up prompt into a session through its owning adapter."""
        session, adapter = self._require_session(session_id)
        self._require_capability("send_prompt", adapter.capabilities.send_prompts)
        try:
            await adapter.send_prompt(self._session_ref(session), prompt)
        except Exception as exc:
            raise await self._operation_failed(
                session.adapter, "send_prompt", exc, session_id=session.id
            ) from exc

    def _require_adapter(self, name: str) -> AgentAdapter:
        adapter = self._adapters.get(name)
        if adapter is None or name not in self._started:
            raise UnknownAdapterError(name)
        return adapter

    def _require_session(self, session_id: str) -> tuple[Session, AgentAdapter]:
        session = self._registry.get(session_id)
        if session is None:
            raise UnknownSessionError(session_id)
        return session, self._require_adapter(session.adapter)

    @staticmethod
    def _require_capability(capability: str, declared: bool) -> None:
        if not declared:
            raise CapabilityNotSupportedError(capability)

    @staticmethod
    def _session_ref(session: Session) -> SessionRef:
        return SessionRef(
            adapter=session.adapter, native_id=session.native_id, session_id=session.id
        )

    async def _operation_failed(
        self, adapter_name: str, operation: str, exc: Exception, *, session_id: str | None = None
    ) -> AdapterOperationError:
        """Report a failed control call; returns the error for the caller to raise.

        ``CapabilityNotSupportedError`` passes through untranslated - it means
        the declared capabilities and the implementation disagree.
        """
        if isinstance(exc, CapabilityNotSupportedError):
            raise exc
        _log.exception("adapter.control_failed", adapter=adapter_name, operation=operation)
        await self._error(adapter_name, f"{operation}_failed", str(exc), session_id=session_id)
        return AdapterOperationError(f"{operation} failed: {exc}")

    # ----------------------------------------------------------- discovery

    async def _discovery_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._discovery_interval)
            for name in list(self._started):
                await self._discover(name, self._adapters[name])

    async def _discover(self, name: str, adapter: AgentAdapter) -> None:
        try:
            descriptors = await adapter.discover_sessions()
        except Exception as exc:
            _log.exception("adapter.discovery_failed", adapter=name)
            await self._error(name, "discovery_failed", str(exc))
            return
        for desc in descriptors:
            try:
                session = await self._registry.upsert_discovered(name, desc)
            except IllegalTransitionError:
                continue  # registry already published adapter.error
            self._ensure_watch(name, adapter, desc.native_id, session.id, session.state)
        await self._bus.publish(
            new_event(
                ev.ADAPTER_DISCOVERY_COMPLETED,
                node=self._node,
                source=f"adapter:{name}",
                payload={"adapter": name, "sessions": len(descriptors)},
            )
        )

    def _ensure_watch(
        self,
        name: str,
        adapter: AgentAdapter,
        native_id: str,
        session_id: str,
        state: SessionState,
    ) -> None:
        """Spawn a supervised watch task for an active, unwatched session."""
        if self._stopping or state in END_STATES:
            return
        key = (name, native_id)
        existing = self._watches.get(key)
        if existing is not None and not existing.done():
            return
        ref = SessionRef(adapter=name, native_id=native_id, session_id=session_id)
        task = asyncio.create_task(
            self._supervised_watch(name, adapter, ref), name=f"watch:{name}:{native_id}"
        )
        self._watches[key] = task

        def _forget(_t: asyncio.Task[None], k: tuple[str, str] = key) -> None:
            self._watches.pop(k, None)

        task.add_done_callback(_forget)

    async def _supervised_watch(self, name: str, adapter: AgentAdapter, ref: SessionRef) -> None:
        backoff = 1.0
        while not self._stopping:
            started = asyncio.get_running_loop().time()
            try:
                await adapter.watch(ref)
                return  # normal return: the adapter decided the session is over
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.exception("adapter.watch_crashed", adapter=name, native_id=ref.native_id)
                await self._error(name, "watch_crashed", str(exc), session_id=ref.session_id)
                ran_for = asyncio.get_running_loop().time() - started
                if ran_for > 60:
                    backoff = 1.0  # it was healthy for a while; restart promptly
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # -------------------------------------------------------- observations

    def _reporter(self, name: str) -> ReportFn:
        async def report(obs: Observation) -> None:
            try:
                await self._handle_observation(name, obs)
            except IllegalTransitionError:
                pass  # registry already published adapter.error
            except Exception as exc:
                _log.exception("adapter.observation_failed", adapter=name)
                await self._error(name, "observation_failed", str(exc))

        return report

    async def _handle_observation(self, name: str, obs: Observation) -> None:
        if isinstance(obs, SessionObservation):
            session = await self._registry.upsert_discovered(name, obs.descriptor)
            self._ensure_watch(
                name, self._adapters[name], obs.descriptor.native_id, session.id, session.state
            )
            return

        resolved = self._resolve(name, obs.native_id)
        if resolved is None and isinstance(obs, InteractionObservation):
            # A freshly launched session may report a permission request in
            # the narrow window before launch() registers it; retry once
            # rather than dropping an interaction a human must see.
            await asyncio.sleep(0.5)
            resolved = self._resolve(name, obs.native_id)
        if resolved is None:
            await self._error(name, "unknown_session", obs.native_id)
            return
        session = resolved
        source = f"adapter:{name}"

        if isinstance(obs, StateObservation):
            await self._registry.observe_state(session.id, obs.state, reason=obs.reason)
        elif isinstance(obs, OutputObservation):
            text = obs.text
            truncated = len(text) > MAX_OUTPUT_CHARS
            payload: dict[str, Any] = {
                "role": obs.role,
                "text": text[:MAX_OUTPUT_CHARS],
                "truncated": truncated,
                **({"at": obs.at.isoformat()} if obs.at else {}),
                **obs.metadata,
            }
            await self._bus.publish(
                new_event(
                    ev.AGENT_OUTPUT_APPENDED,
                    node=self._node,
                    source=source,
                    session_id=session.id,
                    payload=payload,
                )
            )
            self._registry.touch(session.id, obs.at)
        elif isinstance(obs, TurnObservation):
            type_ = (
                ev.AGENT_TURN_STARTED if obs.phase is TurnPhase.STARTED else ev.AGENT_TURN_COMPLETED
            )
            await self._bus.publish(
                new_event(
                    type_,
                    node=self._node,
                    source=source,
                    session_id=session.id,
                    payload={"at": obs.at.isoformat()} if obs.at else {},
                )
            )
            self._registry.touch(session.id, obs.at)
        elif isinstance(obs, ToolObservation):
            await self._bus.publish(
                new_event(
                    _TOOL_EVENT[obs.phase],
                    node=self._node,
                    source=source,
                    session_id=session.id,
                    payload={
                        "tool": obs.tool,
                        "tool_use_id": obs.tool_use_id,
                        "detail": obs.detail,
                        **({"at": obs.at.isoformat()} if obs.at else {}),
                    },
                )
            )
            self._registry.touch(session.id, obs.at)
        elif isinstance(obs, InteractionObservation):
            await self._open_interaction(name, session, obs)
            self._registry.touch(session.id, obs.at)
        elif isinstance(obs, InteractionClosedObservation):
            await self._mediation.cancel_native(
                name, obs.interaction_native_id, reason=obs.reason or "closed_by_agent"
            )
            with contextlib.suppress(IllegalTransitionError):
                await self._registry.observe_state(
                    session.id, SessionState.RUNNING, reason="interaction_closed"
                )

    async def _open_interaction(
        self, name: str, session: Session, obs: InteractionObservation
    ) -> None:
        """Open a mediated interaction and park the session on the human.

        The deliver closure is the answer's route back: mediation calls it with
        the winning answer; it invokes the adapter's ``respond()`` and returns
        the session to ``running``. Its failures are contained here as
        ``adapter.error`` - never propagated into mediation.
        """
        adapter = self._adapters[name]
        answerable = (
            adapter.capabilities.respond_to_permissions
            if obs.kind is InteractionKind.PERMISSION
            else adapter.capabilities.answer_questions
        )
        if not answerable:
            await self._error(
                name, "interaction_capability_missing", obs.kind, session_id=session.id
            )
            return

        async def deliver(interaction: Interaction, answer: Answer) -> None:
            ref = InteractionRef(
                adapter=name,
                session_native_id=obs.native_id,
                interaction_id=interaction.id,
                native_id=obs.interaction_native_id,
            )
            try:
                await adapter.respond(ref, answer)
            except Exception as exc:
                _log.exception("adapter.respond_failed", adapter=name)
                await self._error(name, "respond_failed", str(exc), session_id=session.id)
                return
            await self._resume(session.id, "interaction_resolved")

        await self._open_mediated(
            session,
            InteractionRequest(
                session_id=session.id,
                adapter=name,
                native_id=obs.interaction_native_id,
                kind=obs.kind,
                title=obs.title,
                body=obs.body,
                options=obs.options,
                timeout_s=obs.timeout_s,
            ),
            deliver,
        )

    async def _open_mediated(
        self, session: Session, request: InteractionRequest, deliver: DeliverFn
    ) -> Interaction:
        """Open an interaction and park the session on the human."""
        interaction = await self._mediation.open(request, deliver)
        with contextlib.suppress(IllegalTransitionError):
            await self._registry.observe_state(
                session.id, SessionState.WAITING_ON_USER, reason="interaction_requested"
            )
        return interaction

    async def _resume(self, session_id: str, reason: str) -> None:
        with contextlib.suppress(IllegalTransitionError):
            await self._registry.observe_state(session_id, SessionState.RUNNING, reason=reason)

    # ------------------------------------------------- external interactions

    async def open_external_interaction(
        self,
        *,
        adapter: str,
        session_native_id: str,
        native_id: str,
        kind: InteractionKind,
        title: str,
        body: str = "",
        options: list[str] | None = None,
        timeout_s: float,
    ) -> tuple[Interaction, "asyncio.Future[Interaction]"]:
        """Open an interaction on behalf of an external delivery path.

        The caller (e.g. a blocked hook's HTTP request, ADR-0011) carries the
        resolution back to the agent itself, so there is no capability gate
        and ``adapter.respond()`` is never invoked. The returned future
        resolves with the interaction when mediation delivers a terminal
        status (answered or timed out); cancellation is observable only
        through :meth:`MediationService.get`.
        """
        adapter_obj = self._require_adapter(adapter)
        session = self._resolve(adapter, session_native_id)
        if session is None:
            # The hook for a brand-new session can beat discovery; refresh
            # this adapter's catalogue once rather than dropping the request.
            await self._discover(adapter, adapter_obj)
            session = self._resolve(adapter, session_native_id)
        if session is None:
            raise UnknownSessionError(session_native_id)
        session_id = session.id

        resolved: asyncio.Future[Interaction] = asyncio.get_running_loop().create_future()

        async def deliver(interaction: Interaction, answer: Answer) -> None:
            if not resolved.done():
                resolved.set_result(interaction)
            if interaction.status is InteractionStatus.ANSWERED:
                await self._resume(session_id, "interaction_resolved")
            # On TIMED_OUT the session deliberately stays waiting_on_user:
            # the requester falls through to prompting the human locally, and
            # transcript activity resumes the session naturally.

        interaction = await self._open_mediated(
            session,
            InteractionRequest(
                session_id=session_id,
                adapter=adapter,
                native_id=native_id,
                kind=kind,
                title=title,
                body=body,
                options=list(options or []),
                timeout_s=timeout_s,
            ),
            deliver,
        )
        return interaction, resolved

    async def withdraw_external_interaction(self, interaction_id: str, *, reason: str) -> None:
        """Cancel a pending external interaction (no-op once resolved)."""
        interaction = self._mediation.get(interaction_id)
        if interaction is not None and interaction.status is InteractionStatus.PENDING:
            await self._mediation.cancel_native(
                interaction.adapter, interaction.native_id, reason=reason
            )

    def _resolve(self, name: str, native_id: str) -> Session | None:
        return self._registry.resolve(name, native_id)

    async def _error(
        self, name: str, kind: str, detail: str, *, session_id: str | None = None
    ) -> None:
        await self._bus.publish(
            new_event(
                ev.ADAPTER_ERROR,
                node=self._node,
                source=f"adapter:{name}",
                session_id=session_id,
                payload={"adapter": name, "error": kind, "detail": detail},
            )
        )
