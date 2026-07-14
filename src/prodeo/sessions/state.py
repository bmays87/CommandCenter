"""The canonical session state machine (docs/architecture/event-model.md).

One deliberate extension over the diagram: terminal states other than
``archived`` may transition back to ``running``. Observe-only adapters can
see a session they classified as finished start appending output again
(e.g. a resumed Claude Code session); treating that as a resume is more
truthful than rejecting the observation.
"""

from enum import StrEnum


class SessionState(StrEnum):
    DISCOVERED = "discovered"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_ON_USER = "waiting_on_user"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    ARCHIVED = "archived"


#: States from which no further activity is expected (but see module note).
END_STATES = frozenset(
    {SessionState.COMPLETED, SessionState.FAILED, SessionState.STOPPED, SessionState.ARCHIVED}
)

_LEGAL: dict[SessionState, frozenset[SessionState]] = {
    SessionState.DISCOVERED: frozenset(
        {
            SessionState.STARTING,
            SessionState.RUNNING,
            SessionState.WAITING_ON_USER,
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.STOPPED,
        }
    ),
    SessionState.STARTING: frozenset(
        {SessionState.RUNNING, SessionState.FAILED, SessionState.STOPPED}
    ),
    SessionState.RUNNING: frozenset(
        {
            SessionState.WAITING_ON_USER,
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.STOPPED,
        }
    ),
    SessionState.WAITING_ON_USER: frozenset(
        {
            SessionState.RUNNING,
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.STOPPED,
        }
    ),
    SessionState.COMPLETED: frozenset({SessionState.RUNNING, SessionState.ARCHIVED}),
    SessionState.FAILED: frozenset({SessionState.RUNNING, SessionState.ARCHIVED}),
    SessionState.STOPPED: frozenset({SessionState.RUNNING, SessionState.ARCHIVED}),
    SessionState.ARCHIVED: frozenset(),
}


def can_transition(from_state: SessionState, to_state: SessionState) -> bool:
    """True if the state machine permits ``from_state`` -> ``to_state``."""
    return to_state in _LEGAL[from_state]
