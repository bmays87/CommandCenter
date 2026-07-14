"""Well-known core event type names (Phase 1 subset).

Names follow the taxonomy in docs/architecture/event-model.md:
dot-namespaced, past tense.
"""

from typing import Final

# system
SYSTEM_STARTED: Final = "system.started"
SYSTEM_STOPPING: Final = "system.stopping"
SYSTEM_PLUGIN_LOADED: Final = "system.plugin_loaded"
SYSTEM_PLUGIN_FAILED: Final = "system.plugin_failed"

# session lifecycle. ``session.state_changed`` is emitted for *every*
# transition and is what state is rebuilt from; the specific lifecycle
# events below are additionally emitted for semantic consumers
# (notifications, summaries) so they need not diff states themselves.
SESSION_DISCOVERED: Final = "session.discovered"
SESSION_STARTED: Final = "session.started"
SESSION_STATE_CHANGED: Final = "session.state_changed"
SESSION_COMPLETED: Final = "session.completed"
SESSION_FAILED: Final = "session.failed"
SESSION_STOPPED: Final = "session.stopped"
SESSION_ARCHIVED: Final = "session.archived"

# agent activity
AGENT_OUTPUT_APPENDED: Final = "agent.output_appended"
AGENT_TURN_STARTED: Final = "agent.turn_started"
AGENT_TURN_COMPLETED: Final = "agent.turn_completed"

# tool activity
TOOL_STARTED: Final = "tool.started"
TOOL_FINISHED: Final = "tool.finished"
TOOL_FAILED: Final = "tool.failed"

# adapter lifecycle
ADAPTER_LOADED: Final = "adapter.loaded"
ADAPTER_UNLOADED: Final = "adapter.unloaded"
ADAPTER_ERROR: Final = "adapter.error"
ADAPTER_DISCOVERY_COMPLETED: Final = "adapter.discovery_completed"
