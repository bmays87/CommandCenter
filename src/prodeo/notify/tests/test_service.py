"""Notifier routing: rule matching, fan-out, containment, loop guard."""

import asyncio

import pytest

from prodeo.bus import InProcessEventBus
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.notify import Notification, Notifier


class RecordingChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class ExplodingChannel:
    name = "broken"

    async def send(self, notification: Notification) -> None:
        raise RuntimeError("channel offline")


async def _drain(sub: object) -> list[Event]:
    out: list[Event] = []
    while True:
        try:
            async with asyncio.timeout(0.05):
                async for event in sub:  # type: ignore[attr-defined]
                    out.append(event)
        except TimeoutError:
            return out


async def _settle() -> None:
    """Give the notifier task a chance to process what was published."""
    for _ in range(10):
        await asyncio.sleep(0.01)


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


def _interaction_requested() -> Event:
    return new_event(
        ev.INTERACTION_REQUESTED,
        session_id="sess-1",
        source="mediation",
        payload={"interaction": {"kind": "permission", "title": "Run rm?", "body": "rm -rf"}},
    )


@pytest.mark.asyncio
async def test_matching_events_fan_out_to_routed_channels(bus: InProcessEventBus) -> None:
    ntfy = RecordingChannel("ntfy")
    log = RecordingChannel("log")
    notifier = Notifier(
        bus,
        {"ntfy": ntfy, "log": log},
        {"interaction.requested": ["ntfy", "log"], "session.*": ["log"]},
        public_url="https://cc.example",
    )
    await notifier.start()
    probe = bus.subscribe("notification.*", name="probe")

    await bus.publish(_interaction_requested())
    await bus.publish(new_event(ev.SESSION_COMPLETED, session_id="s2", payload={"title": "T"}))
    await bus.publish(new_event(ev.AGENT_OUTPUT_APPENDED, payload={}))  # no rule
    await _settle()

    assert [n.title for n in ntfy.sent] == ["[permission] Run rm?"]
    assert ntfy.sent[0].priority == "high"
    assert ntfy.sent[0].url == "https://cc.example/#/inbox"
    assert [n.title for n in log.sent] == ["[permission] Run rm?", "Session completed: T"]

    sent = await _drain(probe)
    assert [e.type for e in sent] == [ev.NOTIFICATION_SENT] * 3
    assert {e.payload["channel"] for e in sent} == {"ntfy", "log"}
    await notifier.stop()


@pytest.mark.asyncio
async def test_failing_channel_is_contained_and_others_still_fire(
    bus: InProcessEventBus,
) -> None:
    ok = RecordingChannel("ok")
    notifier = Notifier(
        bus,
        {"broken": ExplodingChannel(), "ok": ok},
        {"interaction.requested": ["broken", "ok"]},
    )
    await notifier.start()
    probe = bus.subscribe("notification.*", name="probe")

    await bus.publish(_interaction_requested())
    await _settle()

    assert len(ok.sent) == 1
    events = await _drain(probe)
    by_type = {e.type for e in events}
    assert by_type == {ev.NOTIFICATION_FAILED, ev.NOTIFICATION_SENT}
    failed = next(e for e in events if e.type == ev.NOTIFICATION_FAILED)
    assert failed.payload["channel"] == "broken"
    assert "channel offline" in failed.payload["error"]
    await notifier.stop()


@pytest.mark.asyncio
async def test_unknown_channel_name_becomes_failed_event(bus: InProcessEventBus) -> None:
    notifier = Notifier(bus, {}, {"interaction.requested": ["ghost"]})
    await notifier.start()
    probe = bus.subscribe("notification.*", name="probe")

    await bus.publish(_interaction_requested())
    await _settle()

    (event,) = await _drain(probe)
    assert event.type == ev.NOTIFICATION_FAILED
    assert event.payload["error"] == "unknown channel"
    await notifier.stop()


@pytest.mark.asyncio
async def test_notification_events_are_never_routed_back(bus: InProcessEventBus) -> None:
    channel = RecordingChannel("all")
    notifier = Notifier(bus, {"all": channel}, {"*": ["all"]})
    await notifier.start()

    await bus.publish(_interaction_requested())
    await _settle()

    # one send for the interaction; the resulting notification.sent event must
    # not have triggered another send (which would cascade forever)
    assert len(channel.sent) == 1
    await notifier.stop()
