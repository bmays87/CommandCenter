"""EventBus interface.

The in-process implementation is the only one in v1; the interface exists so a
broker-backed implementation can be introduced in Phase 5 without touching
services (ADR-0002).
"""

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol

from prodeo.events import Event


class BackpressurePolicy(StrEnum):
    """What to do when a subscriber's queue is full."""

    BLOCK = "block"  # publisher waits (used by the persistence recorder)
    DROP_OLDEST = "drop_oldest"  # best-effort live streams


def matches(pattern: str, event_type: str) -> bool:
    """Match ``session.*`` style patterns against dot-namespaced event types.

    Supported patterns: exact (``session.started``), namespace wildcard
    (``session.*``), and global (``*``).
    """
    if pattern == "*" or pattern == event_type:
        return True
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])
    return False


class Subscription(Protocol):
    """A live subscription; iterate to receive events, close when done."""

    @property
    def name(self) -> str: ...

    def __aiter__(self) -> AsyncIterator[Event]: ...

    async def close(self) -> None: ...


class EventBus(Protocol):
    """Async publish/subscribe bus for immutable domain events."""

    async def publish(self, event: Event) -> None: ...

    def subscribe(
        self,
        pattern: str,
        *,
        name: str,
        policy: BackpressurePolicy = BackpressurePolicy.BLOCK,
        maxsize: int = 1024,
    ) -> Subscription: ...

    async def close(self) -> None: ...
