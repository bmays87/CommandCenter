"""The hub's catalogue of agent machines (Phase 6, ADR-0024)."""

from prodeo.machines.model import Machine
from prodeo.machines.registry import MachineRegistry

__all__ = ["Machine", "MachineRegistry"]
