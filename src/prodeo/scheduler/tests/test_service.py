"""Scheduler lifecycle: create/delete facts, firing, containment, rebuild."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from prodeo.adapters.interface import LaunchSpec
from prodeo.bus import InProcessEventBus
from prodeo.errors import InvalidScheduleError, UnknownScheduleError
from prodeo.events import Event
from prodeo.events import types as ev
from prodeo.persistence import EventRecorder, SqliteEventStore
from prodeo.scheduler import SchedulerService
from prodeo.sessions.model import Session


async def _drain(sub: object) -> list[Event]:
    out: list[Event] = []
    while True:
        try:
            async with asyncio.timeout(0.05):
                async for event in sub:  # type: ignore[attr-defined]
                    out.append(event)
        except TimeoutError:
            return out


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class FakeLauncher:
    """Records launches; optionally fails to test containment."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.launches: list[tuple[str, LaunchSpec]] = []

    async def launch(self, adapter_name: str, spec: LaunchSpec) -> Session:
        self.launches.append((adapter_name, spec))
        if self.fail:
            raise RuntimeError("adapter exploded")
        return Session(id=f"sess-{len(self.launches)}", adapter=adapter_name, native_id="n1")


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 7, 16, 11, 59, 30, tzinfo=UTC))


def _service(
    bus: InProcessEventBus, clock: FakeClock, launcher: FakeLauncher | None = None
) -> tuple[SchedulerService, FakeLauncher]:
    launcher = launcher or FakeLauncher()
    service = SchedulerService(bus, launcher, node="test", tz=UTC, poll_cap_s=0.01, now_fn=clock)
    return service, launcher


@pytest.mark.asyncio
async def test_create_publishes_fact_and_computes_next_run(
    bus: InProcessEventBus, clock: FakeClock
) -> None:
    sub = bus.subscribe("schedule.*", name="probe")
    service, _ = _service(bus, clock)

    schedule = await service.create(
        name="nightly", cron="0 2 * * *", adapter="claude-code", spec=LaunchSpec(prompt="hi")
    )

    assert schedule.next_run_at == datetime(2026, 7, 17, 2, 0, tzinfo=UTC)
    assert service.list_schedules() == [schedule]
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.SCHEDULE_CREATED]
    assert events[0].payload["schedule"]["name"] == "nightly"


@pytest.mark.asyncio
async def test_create_rejects_bad_cron(bus: InProcessEventBus, clock: FakeClock) -> None:
    service, _ = _service(bus, clock)
    with pytest.raises(InvalidScheduleError):
        await service.create(name="x", cron="not cron", adapter="a", spec=LaunchSpec())
    with pytest.raises(InvalidScheduleError):  # parses, but can never fire
        await service.create(name="x", cron="0 0 30 2 *", adapter="a", spec=LaunchSpec())
    assert service.list_schedules() == []


@pytest.mark.asyncio
async def test_delete_publishes_fact_and_unknown_raises(
    bus: InProcessEventBus, clock: FakeClock
) -> None:
    service, _ = _service(bus, clock)
    schedule = await service.create(
        name="nightly", cron="@daily", adapter="claude-code", spec=LaunchSpec()
    )
    sub = bus.subscribe("schedule.*", name="probe")

    await service.delete(schedule.id)

    assert service.list_schedules() == []
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.SCHEDULE_DELETED]
    assert events[0].payload["schedule_id"] == schedule.id
    with pytest.raises(UnknownScheduleError):
        await service.delete(schedule.id)


@pytest.mark.asyncio
async def test_due_schedule_fires_and_reschedules(bus: InProcessEventBus, clock: FakeClock) -> None:
    service, launcher = _service(bus, clock)
    schedule = await service.create(
        name="noon", cron="0 12 * * *", adapter="claude-code", spec=LaunchSpec(prompt="go")
    )
    sub = bus.subscribe("schedule.*", name="probe")

    await service.start()
    try:
        clock.now = datetime(2026, 7, 16, 12, 0, 1, tzinfo=UTC)  # past the slot
        await asyncio.sleep(0.1)
    finally:
        await service.stop()

    assert launcher.launches == [("claude-code", LaunchSpec(prompt="go"))]
    events = await _drain(sub)
    triggered = [e for e in events if e.type == ev.SCHEDULE_TRIGGERED]
    assert len(triggered) == 1
    assert triggered[0].payload["schedule_id"] == schedule.id
    assert triggered[0].session_id == "sess-1"
    assert triggered[0].payload["session_id"] == "sess-1"
    # Rescheduled for the next day, not refired in the same slot.
    assert schedule.next_run_at == datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    assert schedule.last_triggered_at == clock.now


@pytest.mark.asyncio
async def test_launch_failure_is_contained_in_the_event(
    bus: InProcessEventBus, clock: FakeClock
) -> None:
    service, _ = _service(bus, clock, FakeLauncher(fail=True))
    await service.create(name="noon", cron="0 12 * * *", adapter="a", spec=LaunchSpec())
    sub = bus.subscribe("schedule.*", name="probe")

    await service.start()
    try:
        clock.now = datetime(2026, 7, 16, 12, 0, 1, tzinfo=UTC)
        await asyncio.sleep(0.1)
    finally:
        await service.stop()

    events = await _drain(sub)
    triggered = [e for e in events if e.type == ev.SCHEDULE_TRIGGERED]
    assert len(triggered) == 1
    assert triggered[0].payload["error"] == "adapter exploded"
    assert triggered[0].session_id is None
    # The loop survived containment: the schedule has its next slot.
    assert service.list_schedules()[0].next_run_at is not None


@pytest.mark.asyncio
async def test_trigger_now_fires_without_waiting_for_the_slot(
    bus: InProcessEventBus, clock: FakeClock
) -> None:
    service, launcher = _service(bus, clock)
    schedule = await service.create(
        name="nightly", cron="0 2 * * *", adapter="claude-code", spec=LaunchSpec(prompt="go")
    )
    sub = bus.subscribe("schedule.*", name="probe")

    returned = await service.trigger_now(schedule.id)

    assert returned.last_triggered_at == clock.now
    assert launcher.launches == [("claude-code", LaunchSpec(prompt="go"))]
    events = await _drain(sub)
    assert [e.type for e in events] == [ev.SCHEDULE_TRIGGERED]
    with pytest.raises(UnknownScheduleError):
        await service.trigger_now("nope")


@pytest.mark.asyncio
async def test_rebuild_restores_catalogue_and_skips_missed_runs(
    bus: InProcessEventBus, clock: FakeClock, tmp_path: Path
) -> None:
    store = SqliteEventStore(tmp_path / "events.db")
    await store.open()
    recorder = EventRecorder(bus, store)
    await recorder.start()

    service, _ = _service(bus, clock)
    kept = await service.create(name="keep", cron="0 2 * * *", adapter="a", spec=LaunchSpec())
    gone = await service.create(name="gone", cron="@hourly", adapter="a", spec=LaunchSpec())
    await service.delete(gone.id)
    await recorder.stop()

    # Reboot much later: missed runs are skipped, next run computed from now.
    clock.now = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    rebuilt, _ = _service(bus, clock)
    await rebuilt.rebuild(store)

    schedules = rebuilt.list_schedules()
    assert [s.id for s in schedules] == [kept.id]
    assert schedules[0].next_run_at == datetime(2026, 7, 20, 2, 0, tzinfo=UTC)
    await store.close()


@pytest.mark.asyncio
async def test_rebuild_folds_triggered_timestamps(
    bus: InProcessEventBus, clock: FakeClock, tmp_path: Path
) -> None:
    store = SqliteEventStore(tmp_path / "events.db")
    await store.open()
    recorder = EventRecorder(bus, store)
    await recorder.start()

    service, _ = _service(bus, clock)
    schedule = await service.create(name="noon", cron="0 12 * * *", adapter="a", spec=LaunchSpec())
    await service.start()
    clock.now = datetime(2026, 7, 16, 12, 0, 1, tzinfo=UTC)
    await asyncio.sleep(0.1)
    await service.stop()
    await recorder.stop()

    rebuilt, _ = _service(bus, clock)
    await rebuilt.rebuild(store)
    restored = rebuilt.get(schedule.id)
    assert restored is not None
    assert restored.last_triggered_at is not None
    await store.close()
