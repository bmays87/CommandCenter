"""Node sync: mirroring each paired machine's event log into the hub.

One loop per remote machine (ADR-0026): long-poll its CCAN for events after
the hub's per-node cursor, fold session/interaction facts into the hub's
read-models, then republish on the hub bus — which persists them (the store
is idempotent per event id) and pushes them to dashboards. The cursor is
derived, not stored: the newest event the hub already holds for that node.
A machine added moments ago therefore syncs from the beginning of its log,
in its own ULID order, so folds never see a fact before its antecedents.

The loop supervises itself the way adapter watches do: exponential backoff
to a cap while a machine is unreachable, reset on the first good batch.
"""

import asyncio
import contextlib
from typing import Any

import structlog

from prodeo.bus.interface import EventBus
from prodeo.errors import ProdeoError
from prodeo.events import Event
from prodeo.machines.gateway import NodeChannel
from prodeo.machines.registry import MachineRegistry
from prodeo.mediation import MediationService
from prodeo.persistence.interface import EventQuery, EventStore
from prodeo.sessions import SessionRegistry

_log = structlog.get_logger(__name__)

_BACKOFF_CAP_S = 60.0


class NodeSync:
    """Keeps the hub's log and read-models current for every paired machine."""

    def __init__(
        self,
        bus: EventBus,
        store: EventStore,
        registry: SessionRegistry,
        mediation: MediationService,
        machines: MachineRegistry,
        channel: NodeChannel,
        *,
        node: str,
        poll_wait_s: float = 25.0,
        reconcile_interval_s: float = 5.0,
        batch_limit: int = 500,
    ) -> None:
        self._bus = bus
        self._store = store
        self._registry = registry
        self._mediation = mediation
        self._machines = machines
        self._channel = channel
        self._node = node
        self._poll_wait_s = poll_wait_s
        self._reconcile_interval_s = reconcile_interval_s
        self._batch_limit = batch_limit
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._reconcile()
        self._supervisor = asyncio.create_task(self._supervise(), name="node-sync-supervisor")

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        if self._supervisor is not None:
            tasks.append(self._supervisor)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    # -------------------------------------------------------- supervision

    async def _supervise(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._reconcile_interval_s)
            self._reconcile()

    def _reconcile(self) -> None:
        """Match running sync loops to the current machine catalogue."""
        wanted = {m.id: m.node for m in self._machines.list_machines() if m.address is not None}
        for machine_id in list(self._tasks):
            if machine_id not in wanted:
                self._tasks.pop(machine_id).cancel()
        for machine_id, node in wanted.items():
            task = self._tasks.get(machine_id)
            if task is None or task.done():
                self._tasks[machine_id] = asyncio.create_task(
                    self._sync_machine(machine_id, node), name=f"node-sync:{node}"
                )

    # ------------------------------------------------------------ syncing

    async def _sync_machine(self, machine_id: str, node: str) -> None:
        cursor = await self._initial_cursor(node)
        backoff = 1.0
        _log.info("node_sync.started", node=node, cursor=cursor)
        while not self._stopping:
            if self._machines.get(machine_id) is None:  # removed while running
                return
            try:
                batch = await self._fetch(node, cursor)
            except ProdeoError as exc:
                _log.warning("node_sync.unreachable", node=node, error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            backoff = 1.0
            if not batch and self._poll_wait_s <= 0:
                # Long-polling disabled (tests): don't spin on a quiet node.
                await asyncio.sleep(0.05)
            for event in batch:
                if event.node != node:
                    # A CCAN speaks only for itself; drop anything else.
                    _log.warning("node_sync.foreign_event", node=node, event=event.id)
                    continue
                # Fold first, then publish: subscribers who react by
                # re-querying (dashboards) must see the new state.
                self._registry.apply_remote(event)
                self._mediation.apply_remote(event)
                await self._bus.publish(event)
                cursor = event.id

    async def _fetch(self, node: str, cursor: str | None) -> list[Event]:
        after = f"&after={cursor}" if cursor else ""
        data: dict[str, Any] = await self._channel.forward(
            node,
            "GET",
            f"/ccan/v1/events?wait_s={self._poll_wait_s:g}&limit={self._batch_limit}{after}",
        )
        return [Event.model_validate(e) for e in data.get("events", [])]

    async def _initial_cursor(self, node: str) -> str | None:
        newest = await self._store.query(EventQuery(node=node, order="desc", limit=1))
        return newest[0].id if newest else None
