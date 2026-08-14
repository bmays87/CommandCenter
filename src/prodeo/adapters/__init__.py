"""Adapter contract, context, and the Adapter Manager."""

from prodeo.adapters.context import AdapterContext
from prodeo.adapters.interface import (
    ADAPTER_API_VERSION,
    AdapterCapabilities,
    AdapterInfo,
    AdapterMetadata,
    AgentAdapter,
    InteractionRef,
    LaunchSpec,
    ModelInfo,
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
    "AdapterInfo",
    "AdapterManager",
    "AdapterMetadata",
    "AgentAdapter",
    "InteractionClosedObservation",
    "InteractionObservation",
    "InteractionRef",
    "LaunchSpec",
    "ModelInfo",
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
