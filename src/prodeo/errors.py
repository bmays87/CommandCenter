"""Domain exception hierarchy. All prodeo exceptions inherit ``ProdeoError``."""


class ProdeoError(Exception):
    """Base class for all domain errors."""


class IllegalTransitionError(ProdeoError):
    """A session state change violated the canonical state machine."""

    def __init__(self, session_id: str, from_state: str, to_state: str) -> None:
        super().__init__(f"illegal transition {from_state} -> {to_state} for {session_id}")
        self.session_id = session_id
        self.from_state = from_state
        self.to_state = to_state


class UnknownSessionError(ProdeoError):
    """An operation referenced a session the registry does not know."""


class CapabilityNotSupportedError(ProdeoError):
    """A control operation was invoked on an adapter that does not declare it."""
