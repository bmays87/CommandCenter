"""Client presence: who is watching, and are they paying attention.

Presence is deliberately ephemeral - heartbeats are liveness signals, not
durable facts, so they never touch the event log (a heartbeat every few
seconds would drown it). Consumers that need "is anyone attentive right now"
(the Notifier's away-only channel suppression, a voice client deciding
whether to speak) query the tracker or the REST API.
"""

from prodeo.presence.tracker import ClientPresence, PresenceTracker

__all__ = ["ClientPresence", "PresenceTracker"]
