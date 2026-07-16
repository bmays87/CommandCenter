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


class UnknownAdapterError(ProdeoError):
    """An operation referenced an adapter the manager has not loaded."""


class AdapterOperationError(ProdeoError):
    """A control operation failed inside the adapter (contained and reported)."""


class UnknownInteractionError(ProdeoError):
    """An operation referenced an interaction the mediation service does not know."""


class InteractionAlreadyResolvedError(ProdeoError):
    """An interaction was answered after it had already been resolved.

    Resolution is exactly-once: the first answer (or timeout/cancellation) wins.
    """

    def __init__(self, interaction_id: str, status: str) -> None:
        super().__init__(f"interaction {interaction_id} already resolved ({status})")
        self.interaction_id = interaction_id
        self.status = status
