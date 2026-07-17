"""Well-known core event type names.

Names follow the taxonomy in docs/architecture/event-model.md:
dot-namespaced, past tense.
"""

from typing import Final

# system
SYSTEM_STARTED: Final = "system.started"
SYSTEM_STOPPING: Final = "system.stopping"
SYSTEM_PLUGIN_LOADED: Final = "system.plugin_loaded"
SYSTEM_PLUGIN_FAILED: Final = "system.plugin_failed"
SYSTEM_RETENTION_COMPLETED: Final = "system.retention_completed"

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

# interactions ("the agent is blocked on a human"). One mechanism for
# permission requests and questions; the payload's ``kind`` differentiates.
INTERACTION_REQUESTED: Final = "interaction.requested"
INTERACTION_ANSWERED: Final = "interaction.answered"
INTERACTION_TIMED_OUT: Final = "interaction.timed_out"
INTERACTION_CANCELLED: Final = "interaction.cancelled"

# notifications
NOTIFICATION_SENT: Final = "notification.sent"
NOTIFICATION_FAILED: Final = "notification.failed"
NOTIFICATION_SUPPRESSED: Final = "notification.suppressed"

# summaries (periodic activity digests)
SUMMARY_GENERATED: Final = "summary.generated"

# schedules (cron-style agent launches)
SCHEDULE_CREATED: Final = "schedule.created"
SCHEDULE_TRIGGERED: Final = "schedule.triggered"
SCHEDULE_DELETED: Final = "schedule.deleted"

# voice (phase 4). Emitted by voice clients (e.g. Mjölnir) through
# ``POST /api/voice/events``; ``source`` is ``voice:<client_id>``.
VOICE_WAKE_WORD_DETECTED: Final = "voice.wake_word_detected"
VOICE_COMMAND_RECEIVED: Final = "voice.command_received"
VOICE_TRANSCRIPTION_COMPLETED: Final = "voice.transcription_completed"
VOICE_SPEECH_STARTED: Final = "voice.speech_started"
VOICE_SPEECH_FINISHED: Final = "voice.speech_finished"

# adapter lifecycle
ADAPTER_LOADED: Final = "adapter.loaded"
ADAPTER_UNLOADED: Final = "adapter.unloaded"
ADAPTER_ERROR: Final = "adapter.error"
ADAPTER_DISCOVERY_COMPLETED: Final = "adapter.discovery_completed"
