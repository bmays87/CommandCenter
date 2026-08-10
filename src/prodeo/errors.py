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


class UnknownScheduleError(ProdeoError):
    """An operation referenced a schedule the scheduler does not know."""


class InvalidScheduleError(ProdeoError):
    """A schedule definition was rejected (unparsable or never-firing cron)."""


class UnknownInteractionError(ProdeoError):
    """An operation referenced an interaction the mediation service does not know."""


class UnknownExtensionError(ProdeoError):
    """An operation referenced an extension that is not installed."""


class NotEntitledError(ProdeoError):
    """A paid extension was requested without an entitlement (ADR-0015)."""


class RequirementsNotMetError(ProdeoError):
    """An extension declares requirements this machine does not satisfy."""


class UnknownAppError(ProdeoError):
    """An operation referenced an app extension that is not installed."""


class AppNotReadyError(ProdeoError):
    """An app was asked to start before its setup steps were completed.

    Starting anyway would only crash-loop (ADR-0017); the message lists the
    steps, so the caller can show the user what to finish rather than a bare
    exit code.
    """

    def __init__(self, name: str, gaps: list[str]) -> None:
        super().__init__(f"{name!r} is not ready to start: " + "; ".join(gaps))
        self.name = name
        self.gaps = gaps


class InteractionAlreadyResolvedError(ProdeoError):
    """An interaction was answered after it had already been resolved.

    Resolution is exactly-once: the first answer (or timeout/cancellation) wins.
    """

    def __init__(self, interaction_id: str, status: str) -> None:
        super().__init__(f"interaction {interaction_id} already resolved ({status})")
        self.interaction_id = interaction_id
        self.status = status
