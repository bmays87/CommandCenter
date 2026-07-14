"""Registry behavior: discovery, transitions, emitted facts, rebuild."""

import asyncio
from pathlib import Path

import pytest

from prodeo.bus import InProcessEventBus
from prodeo.errors import IllegalTransitionError, UnknownSessionError
from prodeo.events import Event
from prodeo.events import types as ev
from prodeo.persistence import SqliteEventStore
from prodeo.sessions import SessionDescriptor, SessionRegistry, SessionState


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


@pytest.mark.asyncio
async def test_discovery_creates_session_and_emits_facts(bus: InProcessEventBus) -> None:
    sub = bus.subscribe("session.*", name="probe")
    registry = SessionRegistry(bus)

    session = await registry.upsert_discovered(
        "claude-code",
        SessionDescriptor(native_id="abc", title="Fix the bug", project="/repo"),
    )

    assert session.state == SessionState.RUNNING  # descriptor default
    assert registry.get(session.id) is session
    assert registry.resolve("claude-code", "abc") is session

    events = await _drain(sub)
    types = [e.type for e in events]
    assert types == [ev.SESSION_DISCOVERED, ev.SESSION_STATE_CHANGED, ev.SESSION_STARTED]
    assert events[0].payload["session"]["native_id"] == "abc"
    assert events[1].payload == {"from": "discovered", "to": "running", "reason": "discovered"}
    assert all(e.session_id == session.id for e in events)


@pytest.mark.asyncio
async def test_rediscovery_updates_quietly_and_ignores_weak_state_hints(
    bus: InProcessEventBus,
) -> None:
    registry = SessionRegistry(bus)
    first = await registry.upsert_discovered(
        "claude-code", SessionDescriptor(native_id="abc", state=SessionState.COMPLETED)
    )
    await registry.observe_state(first.id, SessionState.ARCHIVED)

    sub = bus.subscribe("*", name="probe")
    again = await registry.upsert_discovered(
        "claude-code",
        SessionDescriptor(native_id="abc", title="New title", state=SessionState.COMPLETED),
    )

    assert again is first
    assert again.title == "New title"
    assert again.state == SessionState.ARCHIVED  # illegal hint ignored, no adapter.error
    assert await _drain(sub) == []


@pytest.mark.asyncio
async def test_terminal_transition_emits_lifecycle_event_and_sets_ended_at(
    bus: InProcessEventBus,
) -> None:
    registry = SessionRegistry(bus)
    session = await registry.upsert_discovered("claude-code", SessionDescriptor(native_id="abc"))
    sub = bus.subscribe("session.*", name="probe")

    await registry.observe_state(session.id, SessionState.COMPLETED, reason="idle")

    assert session.ended_at is not None
    types = [e.type for e in await _drain(sub)]
    assert types == [ev.SESSION_STATE_CHANGED, ev.SESSION_COMPLETED]


@pytest.mark.asyncio
async def test_illegal_transition_emits_adapter_error_and_raises(bus: InProcessEventBus) -> None:
    registry = SessionRegistry(bus)
    session = await registry.upsert_discovered(
        "claude-code", SessionDescriptor(native_id="abc", state=SessionState.COMPLETED)
    )
    await registry.observe_state(session.id, SessionState.ARCHIVED)
    sub = bus.subscribe("adapter.*", name="probe")

    with pytest.raises(IllegalTransitionError):
        await registry.observe_state(session.id, SessionState.RUNNING)

    errors = await _drain(sub)
    assert [e.type for e in errors] == [ev.ADAPTER_ERROR]
    assert errors[0].payload["error"] == "illegal_transition"
    assert session.state == SessionState.ARCHIVED  # state not corrupted


@pytest.mark.asyncio
async def test_unknown_session_raises(bus: InProcessEventBus) -> None:
    registry = SessionRegistry(bus)
    with pytest.raises(UnknownSessionError):
        await registry.observe_state("nope", SessionState.RUNNING)


@pytest.mark.asyncio
async def test_rebuild_restores_catalogue_from_event_log(
    bus: InProcessEventBus, tmp_path: Path
) -> None:
    store = SqliteEventStore(tmp_path / "events.db")
    await store.open()
    sub = bus.subscribe("*", name="recorder")

    registry = SessionRegistry(bus)
    a = await registry.upsert_discovered(
        "claude-code", SessionDescriptor(native_id="a", title="A", project="/a")
    )
    b = await registry.upsert_discovered(
        "claude-code", SessionDescriptor(native_id="b", state=SessionState.COMPLETED)
    )
    await registry.observe_state(a.id, SessionState.COMPLETED, reason="idle")
    for event in await _drain(sub):
        await store.append(event)

    rebuilt = SessionRegistry(bus)
    await rebuilt.rebuild(store)

    assert {s.id for s in rebuilt.list_sessions()} == {a.id, b.id}
    restored_a = rebuilt.get(a.id)
    assert restored_a is not None
    assert restored_a.state == SessionState.COMPLETED
    assert restored_a.title == "A"
    assert restored_a.ended_at is not None
    assert rebuilt.resolve("claude-code", "b") is not None
    await store.close()
