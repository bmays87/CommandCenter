"""Session Registry: the authoritative catalogue of every known agent session.

The registry is the *only* writer of ``session.*`` events. Adapters (via the
Adapter Manager) hand it observations; it applies the canonical state machine
and publishes the resulting facts. It is rebuilt from the event log on boot,
so the in-memory catalogue is a pure fold over ``session.discovered``,
``session.state_changed``, and ``session.updated`` events (the last carries
descriptive facts only — model, title, permission mode, metadata — and its
fold never touches ``state``; state authority stays with
``session.state_changed``).

``last_activity_at`` is updated in memory as activity is observed; after a
rebuild it is approximated by the timestamp of the session's last lifecycle
event (good enough for sorting a fleet view; a StateStore snapshot can refine
this in a later phase).
"""

from datetime import UTC, datetime

import structlog
from ulid import ULID

from prodeo.bus.interface import EventBus
from prodeo.errors import IllegalTransitionError, UnknownSessionError
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.persistence.interface import EventQuery, EventStore
from prodeo.sessions.model import Session, SessionDescriptor
from prodeo.sessions.state import END_STATES, SessionState, can_transition

_log = structlog.get_logger(__name__)

_SOURCE = "session-registry"

_LIFECYCLE_FOR_STATE = {
    SessionState.COMPLETED: ev.SESSION_COMPLETED,
    SessionState.FAILED: ev.SESSION_FAILED,
    SessionState.STOPPED: ev.SESSION_STOPPED,
    SessionState.ARCHIVED: ev.SESSION_ARCHIVED,
}


class SessionRegistry:
    """In-memory catalogue, event-sourced, safe for a single event loop."""

    def __init__(self, bus: EventBus, node: str = "local") -> None:
        self._bus = bus
        self._node = node
        self._by_id: dict[str, Session] = {}
        self._by_native: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------- queries

    def list_sessions(self) -> list[Session]:
        """All known sessions, most recently active first."""
        return sorted(self._by_id.values(), key=lambda s: s.last_activity_at, reverse=True)

    def get(self, session_id: str) -> Session | None:
        return self._by_id.get(session_id)

    def resolve(self, adapter: str, native_id: str) -> Session | None:
        """Find a session by its adapter-native identity."""
        sid = self._by_native.get((adapter, native_id))
        return self._by_id.get(sid) if sid is not None else None

    # ------------------------------------------------------------ commands

    async def upsert_discovered(self, adapter: str, desc: SessionDescriptor) -> Session:
        """Record a discovered session, creating it on first sight.

        Discovery is a weak signal: for already-known sessions it refreshes
        descriptive fields and silently ignores state hints the state machine
        rejects (the per-session watcher is the strong signal).
        """
        existing = self.resolve(adapter, desc.native_id)
        if existing is not None:
            # Diff before mutating: discovery runs on a timer, so publishing
            # unconditionally would flood the log with no-op updates.
            changed: list[str] = []
            if desc.title and desc.title != existing.title:
                existing.title = desc.title
                changed.append("title")
            if desc.model and desc.model != existing.model:
                existing.model = desc.model
                changed.append("model")
            if any(existing.metadata.get(k) != v for k, v in desc.metadata.items()):
                existing.metadata.update(desc.metadata)
                changed.append("metadata")
            if changed:
                await self._publish_updated(existing, changed)
            if desc.state != existing.state:
                # A parked session (waiting_on_user) is blocked on a human via
                # mediation - a block discovery's file-level heuristics cannot
                # see. Only mediation resolution or the watcher's explicit
                # observations un-park it.
                parked = existing.state is SessionState.WAITING_ON_USER
                if not parked and can_transition(existing.state, desc.state):
                    await self.observe_state(existing.id, desc.state, reason="discovery")
                else:
                    _log.debug(
                        "registry.discovery_state_ignored",
                        session_id=existing.id,
                        current=existing.state,
                        hinted=desc.state,
                    )
            return existing

        now = datetime.now(UTC)
        session = Session(
            id=str(ULID()),
            adapter=adapter,
            native_id=desc.native_id,
            title=desc.title,
            project=desc.project,
            model=desc.model,
            state=SessionState.DISCOVERED,
            created_at=desc.started_at or now,
            last_activity_at=desc.last_activity_at or desc.started_at or now,
            metadata=dict(desc.metadata),
        )
        self._by_id[session.id] = session
        self._by_native[(adapter, desc.native_id)] = session.id
        await self._bus.publish(
            new_event(
                ev.SESSION_DISCOVERED,
                node=self._node,
                source=_SOURCE,
                session_id=session.id,
                payload={"session": session.model_dump(mode="json")},
            )
        )
        if desc.state != SessionState.DISCOVERED:
            await self.observe_state(session.id, desc.state, reason="discovered")
        return session

    async def observe_state(
        self, session_id: str, to_state: SessionState, *, reason: str = ""
    ) -> Session:
        """Apply a state transition, publishing the resulting facts.

        Raises :class:`IllegalTransitionError` (after publishing ``adapter.error``)
        when the canonical state machine rejects the change.
        """
        session = self._by_id.get(session_id)
        if session is None:
            raise UnknownSessionError(session_id)
        from_state = session.state
        if to_state == from_state:
            return session
        if not can_transition(from_state, to_state):
            await self._bus.publish(
                new_event(
                    ev.ADAPTER_ERROR,
                    node=self._node,
                    source=_SOURCE,
                    session_id=session_id,
                    payload={
                        "adapter": session.adapter,
                        "error": "illegal_transition",
                        "from": from_state,
                        "to": to_state,
                        "reason": reason,
                    },
                )
            )
            raise IllegalTransitionError(session_id, from_state, to_state)

        now = datetime.now(UTC)
        session.state = to_state
        session.last_activity_at = now
        session.ended_at = now if to_state in END_STATES else None
        await self._bus.publish(
            new_event(
                ev.SESSION_STATE_CHANGED,
                node=self._node,
                source=_SOURCE,
                session_id=session_id,
                payload={"from": from_state, "to": to_state, "reason": reason},
            )
        )
        lifecycle = self._lifecycle_event(from_state, to_state)
        if lifecycle is not None:
            await self._bus.publish(
                new_event(
                    lifecycle,
                    node=self._node,
                    source=_SOURCE,
                    session_id=session_id,
                    payload={"title": session.title, "project": session.project, "reason": reason},
                )
            )
        return session

    def touch(self, session_id: str, at: datetime | None = None) -> None:
        """Record agent activity (keeps fleet-view ordering honest)."""
        session = self._by_id.get(session_id)
        if session is not None:
            session.last_activity_at = at or datetime.now(UTC)

    async def refresh_model(self, session_id: str, model: str) -> None:
        """Record a model change immediately after a control call.

        Descriptive, like discovery's field refreshes. Publishes
        ``session.updated`` so every client converges; the adapter's own
        observations re-confirm (or correct) it as the session's next
        activity is watched.
        """
        session = self._by_id.get(session_id)
        if session is not None and session.model != model:
            session.model = model
            await self._publish_updated(session, ["model"])

    async def refresh_permission_mode(self, session_id: str, mode: str) -> None:
        """Record a permission-mode change immediately after a control call.

        Descriptive, exactly like :meth:`refresh_model`: publishes
        ``session.updated``; the adapter re-confirms on its next observation.
        """
        session = self._by_id.get(session_id)
        if session is not None and session.permission_mode != mode:
            session.permission_mode = mode
            await self._publish_updated(session, ["permission_mode"])

    async def _publish_updated(self, session: Session, fields: list[str]) -> None:
        await self._bus.publish(
            new_event(
                ev.SESSION_UPDATED,
                node=self._node,
                source=_SOURCE,
                session_id=session.id,
                payload={"session": session.model_dump(mode="json"), "fields": fields},
            )
        )

    # ------------------------------------------------------------- rebuild

    async def rebuild(self, store: EventStore) -> None:
        """Reconstruct the catalogue by folding the persisted event log."""
        cursor: str | None = None
        count = 0
        while True:
            batch = await store.query(
                EventQuery(after_id=cursor, type_pattern="session.*", limit=500)
            )
            if not batch:
                break
            for event in batch:
                self._apply(event)
                count += 1
            cursor = batch[-1].id
        _log.info("registry.rebuilt", events=count, sessions=len(self._by_id))

    def _apply(self, event: Event) -> None:
        if event.type == ev.SESSION_DISCOVERED:
            session = Session.model_validate(event.payload["session"])
            self._by_id[session.id] = session
            self._by_native[(session.adapter, session.native_id)] = session.id
        elif event.type == ev.SESSION_STATE_CHANGED:
            existing = self._by_id.get(event.session_id or "")
            if existing is None:
                _log.warning("registry.orphan_state_event", event_id=event.id)
                return
            to_state = SessionState(event.payload["to"])
            existing.state = to_state
            existing.last_activity_at = event.timestamp
            existing.ended_at = event.timestamp if to_state in END_STATES else None
        elif event.type == ev.SESSION_UPDATED:
            existing = self._by_id.get(event.session_id or "")
            if existing is None:
                _log.warning("registry.orphan_updated_event", event_id=event.id)
                return
            # Descriptive fields only. Copying state/ended_at here could
            # resurrect a stale state when an update raced a transition.
            snapshot = Session.model_validate(event.payload["session"])
            existing.title = snapshot.title
            existing.project = snapshot.project
            existing.model = snapshot.model
            existing.permission_mode = snapshot.permission_mode
            existing.metadata = dict(snapshot.metadata)

    @staticmethod
    def _lifecycle_event(from_state: SessionState, to_state: SessionState) -> str | None:
        if to_state == SessionState.RUNNING:
            started_fresh = from_state in (SessionState.DISCOVERED, SessionState.STARTING)
            return ev.SESSION_STARTED if started_fresh else None
        return _LIFECYCLE_FOR_STATE.get(to_state)
