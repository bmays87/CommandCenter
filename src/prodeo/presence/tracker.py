"""In-memory registry of connected clients and their attention state.

Clients (the dashboard, a voice satellite, a phone app) report themselves
with ``PUT /api/presence/{client_id}`` heartbeats carrying an ``attentive``
flag and a TTL; entries that miss their TTL expire silently. Expiry is lazy -
pruned on every read - so the tracker needs no background task.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel


def _now() -> datetime:
    return datetime.now(UTC)


class ClientPresence(BaseModel):
    """One client's latest heartbeat."""

    client_id: str
    #: Free-form client family: ``voice``, ``dashboard``, ``mobile``, ...
    kind: str
    #: Whether the user is actively engaged with this client right now
    #: (dashboard tab focused, voice interaction within its attention window).
    attentive: bool
    node: str = ""
    last_seen: datetime
    expires_at: datetime


class PresenceTracker:
    """Tracks client heartbeats; satisfies the Notifier's ``AttentionSource``."""

    def __init__(self, *, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        self._clients: dict[str, ClientPresence] = {}

    def report(
        self,
        client_id: str,
        *,
        kind: str,
        attentive: bool,
        ttl_s: float,
        node: str = "",
    ) -> ClientPresence:
        """Record one heartbeat, replacing the client's previous entry."""
        now = self._clock()
        presence = ClientPresence(
            client_id=client_id,
            kind=kind,
            attentive=attentive,
            node=node,
            last_seen=now,
            expires_at=now + timedelta(seconds=ttl_s),
        )
        self._clients[client_id] = presence
        return presence

    def forget(self, client_id: str) -> bool:
        """Drop a client immediately (clean shutdown); False if unknown."""
        return self._clients.pop(client_id, None) is not None

    def list_clients(self) -> list[ClientPresence]:
        """Every live (unexpired) client, most recently seen first."""
        self._prune()
        return sorted(self._clients.values(), key=lambda c: c.last_seen, reverse=True)

    def any_attentive(self) -> bool:
        """True when at least one live client reports the user is engaged."""
        self._prune()
        return any(c.attentive for c in self._clients.values())

    def _prune(self) -> None:
        now = self._clock()
        expired = [cid for cid, c in self._clients.items() if c.expires_at <= now]
        for cid in expired:
            del self._clients[cid]
