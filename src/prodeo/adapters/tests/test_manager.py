"""Manager behavior: lifecycle events, translation, containment, supervision."""

import asyncio
from pathlib import Path

import pytest

from prodeo.adapters import (
    AdapterCapabilities,
    AdapterContext,
    AdapterManager,
    AdapterMetadata,
    ObserveOnlyAdapter,
    OutputObservation,
    SessionRef,
    StateObservation,
    ToolObservation,
    ToolPhase,
)
from prodeo.adapters.manager import MAX_OUTPUT_CHARS
from prodeo.bus import InProcessEventBus
from prodeo.events import Event
from prodeo.events import types as ev
from prodeo.sessions import SessionDescriptor, SessionRegistry, SessionState


class ScriptedAdapter(ObserveOnlyAdapter):
    """Test double: discovers one session, reports scripted observations."""

    def __init__(self, name: str = "scripted") -> None:
        self.metadata = AdapterMetadata(name=name, version="0.0.1")
        self.capabilities = AdapterCapabilities(historical_sessions=True)
        self.ctx: AdapterContext | None = None
        self.descriptors = [SessionDescriptor(native_id="n1", title="T", project="/p")]
        self.watched: list[SessionRef] = []
        self.watch_started = asyncio.Event()
        self.watch_forever = asyncio.Event()
        self.stopped = False

    async def start(self, ctx: AdapterContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        self.stopped = True

    async def discover_sessions(self) -> list[SessionDescriptor]:
        return list(self.descriptors)

    async def watch(self, session: SessionRef) -> None:
        self.watched.append(session)
        self.watch_started.set()
        await self.watch_forever.wait()


async def _drain(sub: object) -> list[Event]:
    out: list[Event] = []
    while True:
        try:
            async with asyncio.timeout(0.05):
                async for event in sub:  # type: ignore[attr-defined]
                    out.append(event)
        except TimeoutError:
            return out


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def registry(bus: InProcessEventBus) -> SessionRegistry:
    return SessionRegistry(bus)


def make_manager(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> AdapterManager:
    # discovery_interval=0 disables the periodic loop; tests drive discovery.
    return AdapterManager(bus, registry, data_dir=tmp_path, discovery_interval=0)


@pytest.mark.asyncio
async def test_start_discovers_sessions_and_spawns_watch(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    manager = make_manager(bus, registry, tmp_path)
    manager.add(adapter)
    sub = bus.subscribe("adapter.*", name="probe")

    await manager.start()
    await asyncio.wait_for(adapter.watch_started.wait(), timeout=1)

    types = [e.type for e in await _drain(sub)]
    assert types == [ev.ADAPTER_LOADED, ev.ADAPTER_DISCOVERY_COMPLETED]
    session = registry.resolve("scripted", "n1")
    assert session is not None and session.state == SessionState.RUNNING
    assert adapter.watched[0].session_id == session.id

    await manager.stop()
    assert adapter.stopped


@pytest.mark.asyncio
async def test_completed_sessions_are_not_watched(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    adapter.descriptors = [SessionDescriptor(native_id="old", state=SessionState.COMPLETED)]
    manager = make_manager(bus, registry, tmp_path)
    manager.add(adapter)

    await manager.start()
    await asyncio.sleep(0.05)

    assert adapter.watched == []
    await manager.stop()


@pytest.mark.asyncio
async def test_observations_become_domain_events(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    manager = make_manager(bus, registry, tmp_path)
    manager.add(adapter)
    await manager.start()
    assert adapter.ctx is not None
    session = registry.resolve("scripted", "n1")
    assert session is not None
    sub = bus.subscribe("*", name="probe")

    await adapter.ctx.report(OutputObservation(native_id="n1", role="assistant", text="hi"))
    await adapter.ctx.report(
        ToolObservation(native_id="n1", phase=ToolPhase.STARTED, tool="Bash", tool_use_id="t1")
    )
    await adapter.ctx.report(
        StateObservation(native_id="n1", state=SessionState.COMPLETED, reason="done")
    )

    events = await _drain(sub)
    types = [e.type for e in events]
    assert types == [
        ev.AGENT_OUTPUT_APPENDED,
        ev.TOOL_STARTED,
        ev.SESSION_STATE_CHANGED,
        ev.SESSION_COMPLETED,
    ]
    assert all(e.session_id == session.id for e in events)
    assert events[0].payload["text"] == "hi"
    assert events[0].source == "adapter:scripted"
    assert events[1].payload["tool"] == "Bash"
    await manager.stop()


@pytest.mark.asyncio
async def test_oversized_output_is_truncated(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    manager = make_manager(bus, registry, tmp_path)
    manager.add(adapter)
    await manager.start()
    assert adapter.ctx is not None
    sub = bus.subscribe("agent.*", name="probe")

    await adapter.ctx.report(OutputObservation(native_id="n1", text="x" * (MAX_OUTPUT_CHARS + 5)))

    (event,) = await _drain(sub)
    assert len(event.payload["text"]) == MAX_OUTPUT_CHARS
    assert event.payload["truncated"] is True
    await manager.stop()


@pytest.mark.asyncio
async def test_observation_for_unknown_session_becomes_adapter_error(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    manager = make_manager(bus, registry, tmp_path)
    manager.add(adapter)
    await manager.start()
    assert adapter.ctx is not None
    sub = bus.subscribe("adapter.error", name="probe")

    await adapter.ctx.report(OutputObservation(native_id="ghost", text="?"))

    (event,) = await _drain(sub)
    assert event.payload["error"] == "unknown_session"
    await manager.stop()


@pytest.mark.asyncio
async def test_crashing_adapter_start_is_contained(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    class Exploding(ScriptedAdapter):
        async def start(self, ctx: AdapterContext) -> None:
            raise RuntimeError("boom")

    manager = make_manager(bus, registry, tmp_path)
    manager.add(Exploding("bad"))
    good = ScriptedAdapter("good")
    manager.add(good)
    sub = bus.subscribe("adapter.*", name="probe")

    await manager.start()  # must not raise

    types = [e.type for e in await _drain(sub)]
    assert ev.ADAPTER_ERROR in types
    assert ev.ADAPTER_LOADED in types  # the good adapter still loaded
    assert registry.resolve("good", "n1") is not None
    await manager.stop()


@pytest.mark.asyncio
async def test_crashed_watch_is_restarted(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    class CrashingWatch(ScriptedAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def watch(self, session: SessionRef) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transient")
            self.watch_started.set()
            await self.watch_forever.wait()

    adapter = CrashingWatch()
    manager = make_manager(bus, registry, tmp_path)
    manager.add(adapter)
    sub = bus.subscribe("adapter.error", name="probe")

    await manager.start()
    await asyncio.wait_for(adapter.watch_started.wait(), timeout=5)

    assert adapter.attempts == 2
    errors = await _drain(sub)
    assert [e.payload["error"] for e in errors] == ["watch_crashed"]
    await manager.stop()
