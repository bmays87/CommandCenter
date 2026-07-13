"""Well-known core event type names (Phase 0 subset).

Names follow the taxonomy in docs/architecture/event-model.md:
dot-namespaced, past tense.
"""

from typing import Final

SYSTEM_STARTED: Final = "system.started"
SYSTEM_STOPPING: Final = "system.stopping"
