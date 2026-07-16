"""Adapter contract, context, and the Adapter Manager."""

from prodeo.adapters.context import AdapterContext
from prodeo.adapters.interface import (
    ADAPTER_API_VERSION,
    AdapterCapabilities,
    AdapterMetadata,
    AgentAdapter,
    InteractionRef,
    LaunchSpec,
    ObserveOnlyAdapter,
    SessionRef,
)
from prodeo.adapters.manager import AdapterManager
from prodeo.adapters.observations import (
    InteractionClosedObservation,
    InteractionObservation,
    Observation,
    OutputObservation,
    SessionObservation,
    StateObservation,
    ToolObservation,
    ToolPhase,
    TurnObservation,
    TurnPhase,
)

__all__ = [
    "ADAPTER_API_VERSION",
    "AdapterCapabilities",
    "AdapterContext",
    "AdapterManager",
    "AdapterMetadata",
    "AgentAdapter",
    "InteractionClosedObservation",
    "InteractionObservation",
    "InteractionRef",
    "LaunchSpec",
    "Observation",
    "ObserveOnlyAdapter",
    "OutputObservation",
    "SessionObservation",
    "SessionRef",
    "StateObservation",
    "ToolObservation",
    "ToolPhase",
    "TurnObservation",
    "TurnPhase",
]
