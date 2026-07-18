"""Manager behavior: lifecycle events, translation, containment, supervision."""

import asyncio
from pathlib import Path

import pytest

from prodeo.adapters import (
    AdapterCapabilities,
    AdapterContext,
    AdapterManager,
    AdapterMetadata,
    InteractionClosedObservation,
    InteractionObservation,
    InteractionRef,
    LaunchSpec,
    ObserveOnlyAdapter,
    OutputObservation,
    SessionRef,
    StateObservation,
    ToolObservation,
    ToolPhase,
)
from prodeo.adapters.manager import MAX_OUTPUT_CHARS
from prodeo.bus import InProcessEventBus
from prodeo.errors import (
    AdapterOperationError,
    CapabilityNotSupportedError,
    UnknownAdapterError,
    UnknownSessionError,
)
from prodeo.events import Event
from prodeo.events import types as ev
from prodeo.mediation import Answer, InteractionKind, InteractionStatus, MediationService
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
    bus: InProcessEventBus,
    registry: SessionRegistry,
    tmp_path: Path,
    mediation: MediationService | None = None,
) -> AdapterManager:
    # discovery_interval=0 disables the periodic loop; tests drive discovery.
    return AdapterManager(
        bus, registry, mediation or MediationService(bus), data_dir=tmp_path, discovery_interval=0
    )


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


class ControlAdapter(ScriptedAdapter):
    """Test double with the full control surface, recording every call."""

    def __init__(self, name: str = "controlled") -> None:
        super().__init__(name)
        self.capabilities = AdapterCapabilities(
            launch=True,
            terminate=True,
            respond_to_permissions=True,
            answer_questions=True,
            send_prompts=True,
        )
        self.descriptors = []
        self.launched: list[LaunchSpec] = []
        self.terminated: list[SessionRef] = []
        self.responses: list[tuple[InteractionRef, Answer]] = []
        self.prompts: list[tuple[SessionRef, str]] = []

    async def launch(self, spec: LaunchSpec) -> SessionRef:
        self.launched.append(spec)
        return SessionRef(adapter=self.metadata.name, native_id="launched-1", session_id="")

    async def terminate(self, session: SessionRef) -> None:
        self.terminated.append(session)

    async def respond(self, interaction: InteractionRef, answer: Answer) -> None:
        self.responses.append((interaction, answer))

    async def send_prompt(self, session: SessionRef, prompt: str) -> None:
        self.prompts.append((session, prompt))


@pytest.mark.asyncio
async def test_launch_registers_session_and_watches(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ControlAdapter()
    manager = make_manager(bus, registry, tmp_path)
    manager.add(adapter)
    await manager.start()

    session = await manager.launch("controlled", LaunchSpec(project="/p", prompt="do the thing"))

    assert adapter.launched[0].prompt == "do the thing"
    assert session.native_id == "launched-1"
    assert session.state == SessionState.STARTING
    assert session.metadata["controlled"] == "true"
    await asyncio.wait_for(adapter.watch_started.wait(), timeout=1)
    assert adapter.watched[0].session_id == session.id
    await manager.stop()


@pytest.mark.asyncio
async def test_launch_refused_without_capability_or_adapter(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    manager = make_manager(bus, registry, tmp_path)
    manager.add(ScriptedAdapter())
    await manager.start()

    with pytest.raises(CapabilityNotSupportedError):
        await manager.launch("scripted", LaunchSpec())
    with pytest.raises(UnknownAdapterError):
        await manager.launch("ghost", LaunchSpec())
    await manager.stop()


@pytest.mark.asyncio
async def test_launch_failure_is_contained_and_reported(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    class Exploding(ControlAdapter):
        async def launch(self, spec: LaunchSpec) -> SessionRef:
            raise RuntimeError("no binary")

    manager = make_manager(bus, registry, tmp_path)
    manager.add(Exploding())
    await manager.start()
    sub = bus.subscribe("adapter.error", name="probe")

    with pytest.raises(AdapterOperationError):
        await manager.launch("controlled", LaunchSpec())

    (event,) = await _drain(sub)
    assert event.payload["error"] == "launch_failed"
    await manager.stop()


@pytest.mark.asyncio
async def test_terminate_and_send_prompt_dispatch(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ControlAdapter()
    manager = make_manager(bus, registry, tmp_path)
    manager.add(adapter)
    await manager.start()
    session = await manager.launch("controlled", LaunchSpec(project="/p"))

    await manager.send_prompt(session.id, "and another thing")
    await manager.terminate(session.id)

    assert adapter.prompts[0][1] == "and another thing"
    assert adapter.terminated[0].native_id == "launched-1"
    with pytest.raises(UnknownSessionError):
        await manager.terminate("nope")
    await manager.stop()


@pytest.mark.asyncio
async def test_interaction_observation_opens_mediation_and_answer_routes_back(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ControlAdapter()
    mediation = MediationService(bus)
    manager = make_manager(bus, registry, tmp_path, mediation)
    manager.add(adapter)
    await manager.start()
    session = await manager.launch("controlled", LaunchSpec(project="/p"))
    await registry.observe_state(session.id, SessionState.RUNNING, reason="test")
    assert adapter.ctx is not None
    sub = bus.subscribe("interaction.*", name="probe")

    await adapter.ctx.report(
        InteractionObservation(
            native_id="launched-1",
            interaction_native_id="tool-9",
            kind=InteractionKind.PERMISSION,
            title="Run rm?",
        )
    )

    assert session.state == SessionState.WAITING_ON_USER
    (pending,) = mediation.list_interactions(status=InteractionStatus.PENDING)
    assert pending.session_id == session.id

    await mediation.answer(pending.id, Answer(decision="allow"), answered_by="test")

    ref, answer = adapter.responses[0]
    assert ref.interaction_id == pending.id
    assert ref.native_id == "tool-9"
    assert ref.session_native_id == "launched-1"
    assert answer.decision == "allow"
    resumed = registry.get(session.id)
    assert resumed is not None and resumed.state == SessionState.RUNNING

    types = [e.type for e in await _drain(sub)]
    assert types == [ev.INTERACTION_REQUESTED, ev.INTERACTION_ANSWERED]
    await manager.stop()


@pytest.mark.asyncio
async def test_interaction_without_capability_becomes_adapter_error(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    mediation = MediationService(bus)
    manager = make_manager(bus, registry, tmp_path, mediation)
    manager.add(adapter)
    await manager.start()
    assert adapter.ctx is not None
    sub = bus.subscribe("adapter.error", name="probe")

    await adapter.ctx.report(
        InteractionObservation(
            native_id="n1",
            interaction_native_id="t1",
            kind=InteractionKind.PERMISSION,
            title="?",
        )
    )

    (event,) = await _drain(sub)
    assert event.payload["error"] == "interaction_capability_missing"
    assert mediation.pending_count() == 0
    await manager.stop()


@pytest.mark.asyncio
async def test_interaction_closed_cancels_and_resumes(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ControlAdapter()
    mediation = MediationService(bus)
    manager = make_manager(bus, registry, tmp_path, mediation)
    manager.add(adapter)
    await manager.start()
    session = await manager.launch("controlled", LaunchSpec(project="/p"))
    await registry.observe_state(session.id, SessionState.RUNNING, reason="test")
    assert adapter.ctx is not None

    await adapter.ctx.report(
        InteractionObservation(
            native_id="launched-1",
            interaction_native_id="tool-9",
            kind=InteractionKind.QUESTION,
            title="Which one?",
        )
    )
    assert session.state == SessionState.WAITING_ON_USER

    await adapter.ctx.report(
        InteractionClosedObservation(
            native_id="launched-1", interaction_native_id="tool-9", reason="answered_in_terminal"
        )
    )

    resumed = registry.get(session.id)
    assert resumed is not None and resumed.state == SessionState.RUNNING
    assert adapter.responses == []
    (interaction,) = mediation.list_interactions()
    assert interaction.status == InteractionStatus.CANCELLED
    await manager.stop()


@pytest.mark.asyncio
async def test_external_interaction_opens_on_observe_only_adapter(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    """No capability gate: the external caller carries the answer back itself."""
    adapter = ScriptedAdapter()
    mediation = MediationService(bus)
    manager = make_manager(bus, registry, tmp_path, mediation)
    manager.add(adapter)
    await manager.start()
    session = registry.resolve("scripted", "n1")
    assert session is not None

    interaction, resolved = await manager.open_external_interaction(
        adapter="scripted",
        session_native_id="n1",
        native_id="hook-1",
        kind=InteractionKind.PERMISSION,
        title="Allow Bash?",
        body="{}",
        timeout_s=60,
    )

    assert interaction.session_id == session.id
    assert interaction.status == InteractionStatus.PENDING
    assert not resolved.done()
    assert session.state == SessionState.WAITING_ON_USER
    await manager.stop()


@pytest.mark.asyncio
async def test_external_interaction_rediscovers_unknown_session_then_404s(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    adapter.descriptors = []
    manager = make_manager(bus, registry, tmp_path, MediationService(bus))
    manager.add(adapter)
    await manager.start()

    # A brand-new session's hook can beat discovery: a targeted re-discovery
    # resolves it without waiting for the periodic loop.
    adapter.descriptors = [SessionDescriptor(native_id="fresh")]
    interaction, _resolved = await manager.open_external_interaction(
        adapter="scripted",
        session_native_id="fresh",
        native_id="hook-1",
        kind=InteractionKind.PERMISSION,
        title="Allow Bash?",
        timeout_s=60,
    )
    assert interaction.status == InteractionStatus.PENDING

    with pytest.raises(UnknownSessionError):
        await manager.open_external_interaction(
            adapter="scripted",
            session_native_id="ghost",
            native_id="hook-2",
            kind=InteractionKind.PERMISSION,
            title="Allow Bash?",
            timeout_s=60,
        )
    await manager.stop()


@pytest.mark.asyncio
async def test_external_interaction_answer_resolves_future_without_adapter_respond(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ControlAdapter()
    adapter.descriptors = [SessionDescriptor(native_id="n1")]
    mediation = MediationService(bus)
    manager = make_manager(bus, registry, tmp_path, mediation)
    manager.add(adapter)
    await manager.start()

    interaction, resolved = await manager.open_external_interaction(
        adapter="controlled",
        session_native_id="n1",
        native_id="hook-1",
        kind=InteractionKind.PERMISSION,
        title="Allow Bash?",
        timeout_s=60,
    )
    await mediation.answer(interaction.id, Answer(decision="allow"), answered_by="voice")

    done = await asyncio.wait_for(resolved, timeout=1)
    assert done.status == InteractionStatus.ANSWERED
    assert done.answer is not None and done.answer.decision == "allow"
    assert adapter.responses == []  # the blocked hook delivers, not the adapter
    session = registry.resolve("controlled", "n1")
    assert session is not None and session.state == SessionState.RUNNING
    await manager.stop()


@pytest.mark.asyncio
async def test_external_interaction_timeout_resolves_future_and_stays_waiting(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    mediation = MediationService(bus)
    manager = make_manager(bus, registry, tmp_path, mediation)
    manager.add(adapter)
    await manager.start()

    _interaction, resolved = await manager.open_external_interaction(
        adapter="scripted",
        session_native_id="n1",
        native_id="hook-1",
        kind=InteractionKind.PERMISSION,
        title="Allow Bash?",
        timeout_s=0.05,
    )

    done = await asyncio.wait_for(resolved, timeout=1)
    assert done.status == InteractionStatus.TIMED_OUT
    # The requester now prompts its human locally; the session honestly stays
    # parked until transcript activity resumes it.
    session = registry.resolve("scripted", "n1")
    assert session is not None and session.state == SessionState.WAITING_ON_USER
    await manager.stop()


@pytest.mark.asyncio
async def test_withdraw_external_interaction_cancels_pending_and_noops_resolved(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    adapter = ScriptedAdapter()
    mediation = MediationService(bus)
    manager = make_manager(bus, registry, tmp_path, mediation)
    manager.add(adapter)
    await manager.start()

    interaction, resolved = await manager.open_external_interaction(
        adapter="scripted",
        session_native_id="n1",
        native_id="hook-1",
        kind=InteractionKind.PERMISSION,
        title="Allow Bash?",
        timeout_s=60,
    )
    await manager.withdraw_external_interaction(interaction.id, reason="requester_disconnected")

    current = mediation.get(interaction.id)
    assert current is not None and current.status == InteractionStatus.CANCELLED
    assert not resolved.done()  # cancellation is observed via status, not the future

    # withdrawing again (or after resolution) is a no-op
    await manager.withdraw_external_interaction(interaction.id, reason="requester_disconnected")
    assert current.status == InteractionStatus.CANCELLED
    await manager.stop()


@pytest.mark.asyncio
async def test_failed_respond_is_contained_as_adapter_error(
    bus: InProcessEventBus, registry: SessionRegistry, tmp_path: Path
) -> None:
    class BrokenRespond(ControlAdapter):
        async def respond(self, interaction: InteractionRef, answer: Answer) -> None:
            raise RuntimeError("agent went away")

    adapter = BrokenRespond()
    mediation = MediationService(bus)
    manager = make_manager(bus, registry, tmp_path, mediation)
    manager.add(adapter)
    await manager.start()
    session = await manager.launch("controlled", LaunchSpec(project="/p"))
    await registry.observe_state(session.id, SessionState.RUNNING, reason="test")
    assert adapter.ctx is not None
    await adapter.ctx.report(
        InteractionObservation(
            native_id="launched-1",
            interaction_native_id="t1",
            kind=InteractionKind.PERMISSION,
            title="?",
        )
    )
    (pending,) = mediation.list_interactions(status=InteractionStatus.PENDING)
    sub = bus.subscribe("adapter.error", name="probe")

    answered = await mediation.answer(pending.id, Answer(decision="allow"))

    assert answered.status == InteractionStatus.ANSWERED  # the decision stands
    (event,) = await _drain(sub)
    assert event.payload["error"] == "respond_failed"
    assert session.state == SessionState.WAITING_ON_USER  # honest: agent still blocked
    await manager.stop()
