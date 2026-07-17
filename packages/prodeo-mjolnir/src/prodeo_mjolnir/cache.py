"""The event-stream-fed local cache voice queries read from.

The latency budget (voice-pipeline.md) is why "status" never issues a cold
REST query: the cache takes one snapshot at startup, then folds the live
``session.*`` / ``interaction.*`` stream exactly the way the server's own
registry and mediation service fold the log. Consumers who also want the raw
events (the notification speaker) subscribe with :meth:`subscribe`.
"""

import asyncio
import contextlib

import structlog

from prodeo.events import Event
from prodeo.mediation import Interaction, InteractionStatus
from prodeo.sessions import Session, SessionState
from prodeo_mjolnir.client import ServerClient

_log = structlog.get_logger(__name__)

#: States that count as "the agent is doing (or waiting to do) work".
ACTIVE_STATES = frozenset(
    {SessionState.STARTING, SessionState.RUNNING, SessionState.WAITING_ON_USER}
)

_END_STATES = frozenset(
    {SessionState.COMPLETED, SessionState.FAILED, SessionState.STOPPED, SessionState.ARCHIVED}
)


class LocalCache:
    """Sessions + pending interactions, kept warm by the WebSocket stream."""

    def __init__(self, client: ServerClient) -> None:
        self._client = client
        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, Interaction] = {}
        self._task: asyncio.Task[None] | None = None
        self._subscribers: list[asyncio.Queue[Event]] = []

    async def start(self) -> None:
        """Snapshot over REST, then follow the stream."""
        for session in await self._client.list_sessions():
            self._sessions[session.id] = session
        for interaction in await self._client.list_pending_interactions():
            self._pending[interaction.id] = interaction
        self._task = asyncio.create_task(self._follow(), name="mjolnir-cache")
        _log.info("cache.started", sessions=len(self._sessions), pending=len(self._pending))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -------------------------------------------------------------- queries

    def sessions(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.last_activity_at, reverse=True)

    def active_sessions(self) -> list[Session]:
        return [s for s in self.sessions() if s.state in ACTIVE_STATES]

    def pending_interactions(self) -> list[Interaction]:
        return sorted(self._pending.values(), key=lambda i: i.requested_at)

    def session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def subscribe(self) -> asyncio.Queue[Event]:
        """A queue of every event the cache sees (session.* / interaction.*)."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        self._subscribers.append(queue)
        return queue

    # ---------------------------------------------------------------- fold

    async def _follow(self) -> None:
        async for event in self._client.stream_events(["session.*", "interaction.*"]):
            try:
                self._apply(event)
            except Exception:  # a malformed event must not kill the feed
                _log.exception("cache.apply_failed", event_id=event.id)
            for queue in self._subscribers:
                if not queue.full():  # slow consumer: drop, queries still fresh
                    queue.put_nowait(event)

    def _apply(self, event: Event) -> None:
        if event.type == "session.discovered":
            session = Session.model_validate(event.payload["session"])
            self._sessions[session.id] = session
        elif event.type == "session.state_changed":
            existing = self._sessions.get(event.session_id or "")
            if existing is None:
                return
            to_state = SessionState(event.payload["to"])
            existing.state = to_state
            existing.last_activity_at = event.timestamp
            existing.ended_at = event.timestamp if to_state in _END_STATES else None
        elif event.type == "interaction.requested":
            interaction = Interaction.model_validate(event.payload["interaction"])
            if interaction.status == InteractionStatus.PENDING:
                self._pending[interaction.id] = interaction
        elif event.type in (
            "interaction.answered",
            "interaction.timed_out",
            "interaction.cancelled",
        ):
            self._pending.pop(str(event.payload.get("interaction_id", "")), None)
