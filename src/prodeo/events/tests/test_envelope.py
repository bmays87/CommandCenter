"""Envelope invariants: immutability, ULID ordering, defaults."""

import pydantic
import pytest

from prodeo.events import Event, new_event


def test_ids_are_chronologically_sortable() -> None:
    events = [new_event("system.started") for _ in range(50)]
    ids = [e.id for e in events]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_events_are_frozen() -> None:
    event = new_event("system.started")
    with pytest.raises(pydantic.ValidationError):
        event.type = "mutated"  # type: ignore[misc]


def test_timestamp_is_utc() -> None:
    event = new_event("system.started")
    assert event.timestamp.utcoffset() is not None
    assert event.timestamp.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_round_trips_through_json() -> None:
    event = new_event("tool.finished", payload={"exit_code": 0}, session_id="cc-1")
    restored = Event.model_validate_json(event.model_dump_json())
    assert restored == event
