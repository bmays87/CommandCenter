"""Machine Registry: the hub's catalogue of agent machines.

One record per machine that runs (or will run) coding agents. The registry is
the *only* writer of ``machine.*`` events and folds them back on boot, so a
tab rename is a durable fact every dashboard client sees, not browser-local
state. The hub's own machine registers itself on first boot
(:meth:`MachineRegistry.ensure_local`), so a single-machine deployment
upgrades into a one-tab fleet rather than an empty one.

Remote machines join by pairing with a CCAN at an FQDN/IP; the handshake
itself arrives with the CCAN package (Phase 6 workstream B), so until then
:meth:`MachineRegistry.add` is exercised only by ``ensure_local`` and tests.
"""

import structlog
from ulid import ULID

from prodeo.bus.interface import EventBus
from prodeo.errors import MachineConflictError, UnknownMachineError
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.machines.model import Machine
from prodeo.persistence.interface import EventQuery, EventStore

_log = structlog.get_logger(__name__)

_SOURCE = "machine-registry"


class MachineRegistry:
    """In-memory catalogue, event-sourced, safe for a single event loop."""

    def __init__(self, bus: EventBus, node: str = "local") -> None:
        self._bus = bus
        self._node = node
        self._by_id: dict[str, Machine] = {}

    # ------------------------------------------------------------- queries

    def list_machines(self) -> list[Machine]:
        """All known machines, first added first — the dashboard's tab order."""
        return sorted(self._by_id.values(), key=lambda m: m.id)

    def get(self, machine_id: str) -> Machine | None:
        return self._by_id.get(machine_id)

    def get_by_node(self, node: str) -> Machine | None:
        """Find a machine by its node identity."""
        return next((m for m in self._by_id.values() if m.node == node), None)

    # ------------------------------------------------------------ commands

    async def add(self, *, node: str, name: str = "", address: str | None = None) -> Machine:
        """Register a machine (409 upstream when its node is already known)."""
        if self.get_by_node(node) is not None:
            raise MachineConflictError(f"machine {node!r} is already registered")
        machine = Machine(id=str(ULID()), node=node, name=name or node, address=address)
        self._by_id[machine.id] = machine
        await self._bus.publish(
            new_event(
                ev.MACHINE_ADDED,
                node=self._node,
                source=_SOURCE,
                payload={"machine": machine.model_dump(mode="json")},
            )
        )
        return machine

    async def ensure_local(self) -> Machine:
        """Register the hub's own machine; idempotent across boots.

        Idempotence comes from the rebuild that precedes it: once the
        ``machine.added`` fact is in the log, every later boot finds the
        record and returns it unchanged.
        """
        existing = self.get_by_node(self._node)
        if existing is not None:
            return existing
        return await self.add(node=self._node)

    async def rename(self, machine_id: str, name: str) -> Machine:
        """Change the display name; node identity is untouchable by design."""
        machine = self._by_id.get(machine_id)
        if machine is None:
            raise UnknownMachineError(f"unknown machine: {machine_id}")
        machine.name = name
        await self._bus.publish(
            new_event(
                ev.MACHINE_RENAMED,
                node=self._node,
                source=_SOURCE,
                payload={"machine_id": machine_id, "name": name},
            )
        )
        return machine

    async def remove(self, machine_id: str) -> None:
        """Forget a machine; its sessions and events stay in the log."""
        machine = self._by_id.get(machine_id)
        if machine is None:
            raise UnknownMachineError(f"unknown machine: {machine_id}")
        if machine.address is None:
            raise MachineConflictError("the hub's own machine cannot be removed")
        del self._by_id[machine_id]
        await self._bus.publish(
            new_event(
                ev.MACHINE_REMOVED,
                node=self._node,
                source=_SOURCE,
                payload={"machine_id": machine_id},
            )
        )

    # ------------------------------------------------------------- rebuild

    async def rebuild(self, store: EventStore) -> None:
        """Fold the persisted ``machine.*`` log."""
        cursor: str | None = None
        count = 0
        while True:
            batch = await store.query(
                EventQuery(after_id=cursor, type_pattern="machine.*", limit=500)
            )
            if not batch:
                break
            cursor = batch[-1].id
            for event in batch:
                self._apply(event)
                count += 1
        _log.info("machines.rebuilt", events=count, machines=len(self._by_id))

    def _apply(self, event: Event) -> None:
        if event.type == ev.MACHINE_ADDED:
            machine = Machine.model_validate(event.payload["machine"])
            self._by_id[machine.id] = machine
        elif event.type == ev.MACHINE_RENAMED:
            machine_or_none = self._by_id.get(event.payload["machine_id"])
            if machine_or_none is not None:
                machine_or_none.name = event.payload["name"]
        elif event.type == ev.MACHINE_REMOVED:
            self._by_id.pop(event.payload["machine_id"], None)
