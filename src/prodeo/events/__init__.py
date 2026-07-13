"""Event envelope and core event types.

Events are the system's contract. See docs/architecture/event-model.md for the
taxonomy, naming rules, and versioning policy.
"""

from prodeo.events.envelope import Event, new_event

__all__ = ["Event", "new_event"]
