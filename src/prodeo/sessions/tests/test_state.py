"""State machine rules: the diagram in event-model.md plus the resume extension."""

import pytest

from prodeo.sessions.state import SessionState, can_transition


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (SessionState.DISCOVERED, SessionState.STARTING),
        (SessionState.DISCOVERED, SessionState.RUNNING),
        (SessionState.DISCOVERED, SessionState.COMPLETED),  # historical discovery
        (SessionState.STARTING, SessionState.RUNNING),
        (SessionState.RUNNING, SessionState.WAITING_ON_USER),
        (SessionState.WAITING_ON_USER, SessionState.RUNNING),
        (SessionState.RUNNING, SessionState.COMPLETED),
        (SessionState.RUNNING, SessionState.FAILED),
        (SessionState.RUNNING, SessionState.STOPPED),
        (SessionState.COMPLETED, SessionState.ARCHIVED),
        (SessionState.COMPLETED, SessionState.RUNNING),  # resumed session
    ],
)
def test_legal_transitions(from_state: SessionState, to_state: SessionState) -> None:
    assert can_transition(from_state, to_state)


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (SessionState.ARCHIVED, SessionState.RUNNING),
        (SessionState.ARCHIVED, SessionState.ARCHIVED),
        (SessionState.DISCOVERED, SessionState.ARCHIVED),
        (SessionState.RUNNING, SessionState.STARTING),
        (SessionState.COMPLETED, SessionState.FAILED),
    ],
)
def test_illegal_transitions(from_state: SessionState, to_state: SessionState) -> None:
    assert not can_transition(from_state, to_state)
