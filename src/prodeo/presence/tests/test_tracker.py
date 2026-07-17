"""PresenceTracker: heartbeats, lazy expiry, attention queries."""

from datetime import UTC, datetime, timedelta

from prodeo.presence import PresenceTracker


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 16, 7, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_report_and_list() -> None:
    clock = FakeClock()
    tracker = PresenceTracker(clock=clock)
    tracker.report("mjolnir", kind="voice", attentive=True, ttl_s=30, node="kitchen-pi")
    clock.advance(1)
    tracker.report("dashboard-abc", kind="dashboard", attentive=False, ttl_s=30)

    clients = tracker.list_clients()
    assert [c.client_id for c in clients] == ["dashboard-abc", "mjolnir"]
    assert clients[1].node == "kitchen-pi"
    assert clients[1].expires_at == clients[1].last_seen + timedelta(seconds=30)


def test_heartbeat_replaces_previous_entry() -> None:
    clock = FakeClock()
    tracker = PresenceTracker(clock=clock)
    tracker.report("mjolnir", kind="voice", attentive=True, ttl_s=30)
    tracker.report("mjolnir", kind="voice", attentive=False, ttl_s=30)
    clients = tracker.list_clients()
    assert len(clients) == 1
    assert clients[0].attentive is False


def test_expiry_is_lazy_and_silent() -> None:
    clock = FakeClock()
    tracker = PresenceTracker(clock=clock)
    tracker.report("mjolnir", kind="voice", attentive=True, ttl_s=30)
    clock.advance(31)
    assert tracker.list_clients() == []
    assert tracker.any_attentive() is False


def test_any_attentive() -> None:
    clock = FakeClock()
    tracker = PresenceTracker(clock=clock)
    assert tracker.any_attentive() is False
    tracker.report("dashboard-abc", kind="dashboard", attentive=False, ttl_s=30)
    assert tracker.any_attentive() is False
    tracker.report("mjolnir", kind="voice", attentive=True, ttl_s=30)
    assert tracker.any_attentive() is True
    clock.advance(31)
    assert tracker.any_attentive() is False


def test_forget() -> None:
    tracker = PresenceTracker()
    tracker.report("mjolnir", kind="voice", attentive=True, ttl_s=30)
    assert tracker.forget("mjolnir") is True
    assert tracker.forget("mjolnir") is False
    assert tracker.list_clients() == []
