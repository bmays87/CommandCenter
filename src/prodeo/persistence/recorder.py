"""EventRecorder: the bus subscriber that makes events durable.

Subscribes to ``*`` with BLOCK backpressure - persistence is the one consumer
that is allowed to slow publishers down (at-least-once, ADR-0002).
"""

import asyncio

import structlog

from prodeo.bus.interface import BackpressurePolicy, EventBus, Subscription
from prodeo.persistence.interface import EventStore

_log = structlog.get_logger(__name__)


class EventRecorder:
    """Owns the long-running task copying the bus into the event store."""

    def __init__(self, bus: EventBus, store: EventStore) -> None:
        self._bus = bus
        self._store = store
        self._task: asyncio.Task[None] | None = None
        self._sub: Subscription | None = None

    async def start(self) -> None:
        self._sub = self._bus.subscribe("*", name="event-recorder", policy=BackpressurePolicy.BLOCK)
        self._task = asyncio.create_task(self._run(), name="event-recorder")

    async def _run(self) -> None:
        assert self._sub is not None
        async for event in self._sub:
            try:
                await self._store.append(event)
            except Exception:
                _log.exception("recorder.append_failed", event_id=event.id)

    async def stop(self) -> None:
        if self._sub is not None:
            await self._sub.close()
        if self._task is not None:
            await self._task
            self._task = None
