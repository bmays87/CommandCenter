"""Machine catalogue: add/rename/remove facts, local guard, rebuild."""

import asyncio
from pathlib import Path

import pytest

from prodeo.bus import InProcessEventBus
from prodeo.errors import MachineConflictError, UnknownMachineError
from prodeo.events import Event
from prodeo.events import types as ev
from prodeo.machines import MachineRegistry
from prodeo.persistence import EventRecorder, SqliteEventStore


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
async def test_add_publishes_fact_with_full_dump(bus: InProcessEventBus) -> None:
    sub = bus.subscribe("machine.*", name="probe")
    registry = MachineRegistry(bus, node="hub")

    machine = await registry.add(node="worker-01", address="worker-01.lan")

    events = await _drain(sub)
    assert [e.type for e in events] == [ev.MACHINE_ADDED]
    assert events[0].payload["machine"]["id"] == machine.id
    assert events[0].payload["machine"]["node"] == "worker-01"
    assert events[0].node == "hub"
    # The display name defaults to the node identity.
    assert machine.name == "worker-01"
    assert registry.get(machine.id) == machine
    assert registry.get_by_node("worker-01") == machine


@pytest.mark.asyncio
async def test_add_rejects_a_node_registered_twice(bus: InProcessEventBus) -> None:
    registry = MachineRegistry(bus, node="hub")
    await registry.add(node="worker-01", address="a.lan")

    with pytest.raises(MachineConflictError):
        await registry.add(node="worker-01", address="b.lan")


@pytest.mark.asyncio
async def test_ensure_local_registers_once(bus: InProcessEventBus) -> None:
    sub = bus.subscribe("machine.*", name="probe")
    registry = MachineRegistry(bus, node="hub")

    first = await registry.ensure_local()
    second = await registry.ensure_local()

    assert first.id == second.id
    assert first.node == "hub"
    assert first.address is None
    assert [e.type for e in await _drain(sub)] == [ev.MACHINE_ADDED]


@pytest.mark.asyncio
async def test_rename_changes_display_name_only(bus: InProcessEventBus) -> None:
    registry = MachineRegistry(bus, node="hub")
    machine = await registry.add(node="worker-01", address="a.lan")
    sub = bus.subscribe("machine.*", name="probe")

    renamed = await registry.rename(machine.id, "GPU box")

    assert renamed.name == "GPU box"
    assert renamed.node == "worker-01"
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.MACHINE_RENAMED]
    assert events[0].payload == {"machine_id": machine.id, "name": "GPU box"}


@pytest.mark.asyncio
async def test_rename_unknown_machine_raises(bus: InProcessEventBus) -> None:
    registry = MachineRegistry(bus, node="hub")
    with pytest.raises(UnknownMachineError):
        await registry.rename("nope", "x")


@pytest.mark.asyncio
async def test_remove_forgets_the_machine(bus: InProcessEventBus) -> None:
    registry = MachineRegistry(bus, node="hub")
    machine = await registry.add(node="worker-01", address="a.lan")
    sub = bus.subscribe("machine.*", name="probe")

    await registry.remove(machine.id)

    assert registry.get(machine.id) is None
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.MACHINE_REMOVED]
    assert events[0].payload == {"machine_id": machine.id}
    with pytest.raises(UnknownMachineError):
        await registry.remove(machine.id)


@pytest.mark.asyncio
async def test_the_hubs_own_machine_cannot_be_removed(bus: InProcessEventBus) -> None:
    registry = MachineRegistry(bus, node="hub")
    local = await registry.ensure_local()

    with pytest.raises(MachineConflictError):
        await registry.remove(local.id)


@pytest.mark.asyncio
async def test_list_is_in_tab_order_first_added_first(bus: InProcessEventBus) -> None:
    registry = MachineRegistry(bus, node="hub")
    local = await registry.ensure_local()
    second = await registry.add(node="worker-01", address="a.lan")
    third = await registry.add(node="worker-02", address="b.lan")

    assert [m.id for m in registry.list_machines()] == [local.id, second.id, third.id]


@pytest.mark.asyncio
async def test_rebuild_restores_the_catalogue(bus: InProcessEventBus, tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "events.db")
    await store.open()
    recorder = EventRecorder(bus, store)
    await recorder.start()

    registry = MachineRegistry(bus, node="hub")
    local = await registry.ensure_local()
    kept = await registry.add(node="worker-01", address="a.lan")
    await registry.rename(kept.id, "GPU box")
    gone = await registry.add(node="worker-02", address="b.lan")
    await registry.remove(gone.id)
    await recorder.stop()

    rebuilt = MachineRegistry(bus, node="hub")
    await rebuilt.rebuild(store)

    machines = rebuilt.list_machines()
    assert [m.id for m in machines] == [local.id, kept.id]
    assert machines[1].name == "GPU box"
    # ensure_local after rebuild finds the record instead of re-adding.
    assert (await rebuilt.ensure_local()).id == local.id
    await store.close()
