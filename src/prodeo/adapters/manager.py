"""AdapterManager: loads adapters, supervises their tasks, translates
observations into domain events.

Containment is the job here: an adapter exception must never take down the
manager or corrupt the event stream. Every call into adapter code is guarded;
failures become ``adapter.error`` events and (for watch tasks) supervised
restarts with exponential backoff.
"""

import asyncio
import contextlib
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import structlog

from prodeo.adapters.context import AdapterContext, ReportFn
from prodeo.adapters.interface import ADAPTER_API_VERSION, AgentAdapter, SessionRef
from prodeo.adapters.observations import (
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
from prodeo.errors import IllegalTransitionError
from prodeo.events import new_event
from prodeo.events import types as ev
from prodeo.sessions import Session, SessionRegistry
from prodeo.sessions.state import END_STATES, SessionState

_log = structlog.get_logger(__name__)

PLUGIN_ENTRY_POINT_GROUP = "prodeo.plugins"

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
        *,
        data_dir: Path,
        node: str = "local",
        adapter_config: dict[str, dict[str, Any]] | None = None,
        discovery_interval: float = 10.0,
    ) -> None:
        self._bus = bus
        self._registry = registry
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
        """Register an adapter instance directly (tests, embedded use)."""
        self._adapters[adapter.metadata.name] = adapter

    async def load_entry_points(self) -> None:
        """Discover adapter plugins via the ``prodeo.plugins`` entry point group.

        Each entry point must resolve to a zero-argument factory returning an
        :class:`AgentAdapter`. Incompatible or broken plugins are reported and
        skipped - never fatal (ADR-0005).
        """
        for ep in entry_points(group=PLUGIN_ENTRY_POINT_GROUP):
            try:
                factory = ep.load()
                adapter: AgentAdapter = factory()
                declared = adapter.metadata.adapter_api_version
                if declared != ADAPTER_API_VERSION:
                    raise RuntimeError(
                        f"adapter API version mismatch: plugin declares {declared}, "
                        f"core provides {ADAPTER_API_VERSION}"
                    )
            except Exception as exc:
                _log.exception("adapter.plugin_load_failed", entry_point=ep.name)
                await self._bus.publish(
                    new_event(
                        ev.SYSTEM_PLUGIN_FAILED,
                        node=self._node,
                        source="adapter-manager",
                        payload={"plugin": ep.name, "error": str(exc)},
                    )
                )
                continue
            self.add(adapter)
            await self._bus.publish(
                new_event(
                    ev.SYSTEM_PLUGIN_LOADED,
                    node=self._node,
                    source="adapter-manager",
                    payload={"plugin": ep.name, "kind": "adapter"},
                )
            )

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
