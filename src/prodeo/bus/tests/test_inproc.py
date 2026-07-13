"""In-process bus behavior: fan-out, patterns, backpressure, close."""

import asyncio

import pytest

from prodeo.bus import BackpressurePolicy, InProcessEventBus
from prodeo.bus.interface import matches
from prodeo.events import Event, new_event


@pytest.mark.parametrize(
    ("pattern", "event_type", "expected"),
    [
        ("*", "session.started", True),
        ("session.started", "session.started", True),
        ("session.*", "session.started", True),
        ("session.*", "tool.started", False),
        ("session.started", "session.stopped", False),
        ("session.*", "session", False),
    ],
)
def test_pattern_matching(pattern: str, event_type: str, expected: bool) -> None:
    assert matches(pattern, event_type) is expected


async def _collect(sub: object, n: int) -> list[Event]:
    out: list[Event] = []
    async for event in sub:  # type: ignore[attr-defined]
        out.append(event)
        if len(out) == n:
            break
    return out


@pytest.mark.asyncio
async def test_fan_out_to_matching_subscribers() -> None:
    bus = InProcessEventBus()
    all_sub = bus.subscribe("*", name="all")
    session_sub = bus.subscribe("session.*", name="session")

    await bus.publish(new_event("session.started"))
    await bus.publish(new_event("tool.started"))

    got_all = await asyncio.wait_for(_collect(all_sub, 2), timeout=1)
    got_session = await asyncio.wait_for(_collect(session_sub, 1), timeout=1)

    assert [e.type for e in got_all] == ["session.started", "tool.started"]
    assert [e.type for e in got_session] == ["session.started"]
    await bus.close()


@pytest.mark.asyncio
async def test_slow_subscriber_with_drop_oldest_never_blocks_publisher() -> None:
    bus = InProcessEventBus()
    sub = bus.subscribe("*", name="slow", policy=BackpressurePolicy.DROP_OLDEST, maxsize=2)

    for i in range(10):
        await asyncio.wait_for(bus.publish(new_event("tool.started", payload={"i": i})), timeout=1)

    received = await asyncio.wait_for(_collect(sub, 2), timeout=1)
    assert [e.payload["i"] for e in received] == [8, 9]  # oldest dropped
    await bus.close()


@pytest.mark.asyncio
async def test_closed_subscription_stops_iteration_and_detaches() -> None:
    bus = InProcessEventBus()
    sub = bus.subscribe("*", name="temp")
    await sub.close()

    collected = await asyncio.wait_for(_collect(sub, 0), timeout=1)
    assert collected == []
    await bus.publish(new_event("system.started"))  # no subscribers: no error
    await bus.close()


@pytest.mark.asyncio
async def test_publish_after_close_raises() -> None:
    bus = InProcessEventBus()
    await bus.close()
    with pytest.raises(RuntimeError):
        await bus.publish(new_event("system.started"))
