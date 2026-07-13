"""In-process asyncio event bus (the v1 default)."""

import asyncio
from collections.abc import AsyncIterator

import structlog

from prodeo.bus.interface import BackpressurePolicy, matches
from prodeo.events import Event

_log = structlog.get_logger(__name__)

_CLOSE = object()


class _QueueSubscription:
    """Per-subscriber queue; slow subscribers never block other subscribers."""

    def __init__(
        self,
        bus: "InProcessEventBus",
        pattern: str,
        name: str,
        policy: BackpressurePolicy,
        maxsize: int,
    ) -> None:
        self._bus = bus
        self.pattern = pattern
        self._name = name
        self.policy = policy
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    async def _deliver(self, event: Event) -> None:
        if self._closed:
            return
        if self.policy is BackpressurePolicy.BLOCK:
            await self._queue.put(event)
            return
        # DROP_OLDEST: make room, never block the publisher.
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    dropped = self._queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race window
                    continue
                if dropped is not _CLOSE and isinstance(dropped, Event):
                    _log.warning(
                        "bus.subscriber_dropped_event",
                        subscriber=self._name,
                        dropped_id=dropped.id,
                    )

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Event]:
        while True:
            item = await self._queue.get()
            if item is _CLOSE:
                return
            assert isinstance(item, Event)
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._detach(self)
        await self._queue.put(_CLOSE)


class InProcessEventBus:
    """Simple, ordered fan-out bus for a single process."""

    def __init__(self) -> None:
        self._subs: list[_QueueSubscription] = []
        self._closed = False

    async def publish(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("bus is closed")
        for sub in list(self._subs):
            if matches(sub.pattern, event.type):
                await sub._deliver(event)

    def subscribe(
        self,
        pattern: str,
        *,
        name: str,
        policy: BackpressurePolicy = BackpressurePolicy.BLOCK,
        maxsize: int = 1024,
    ) -> _QueueSubscription:
        if self._closed:
            raise RuntimeError("bus is closed")
        sub = _QueueSubscription(self, pattern, name, policy, maxsize)
        self._subs.append(sub)
        return sub

    def _detach(self, sub: _QueueSubscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    async def close(self) -> None:
        self._closed = True
        for sub in list(self._subs):
            await sub.close()
