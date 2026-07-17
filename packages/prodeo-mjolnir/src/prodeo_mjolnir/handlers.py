"""Intent execution: cache queries + REST commands -> speakable text.

Reads come from the :class:`LocalCache` (latency budget); writes go through
the :class:`ServerClient` and the server's mediation rules apply - if a
simultaneous dashboard click wins the interaction, the voice user is told so
(``already_resolved``) rather than pretending success.
"""

import re
from datetime import UTC, datetime, timedelta

import structlog

from prodeo.mediation import Interaction
from prodeo.sessions import Session, SessionState
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.client import ServerClient
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.errors import AlreadyResolvedError, MjolnirError
from prodeo_mjolnir.intents import (
    ApproveIntent,
    CancelIntent,
    DenyIntent,
    HelpIntent,
    Intent,
    OvernightIntent,
    PendingIntent,
    StatusIntent,
    StopIntent,
    UnknownIntent,
    normalize,
)

_log = structlog.get_logger(__name__)


def speakable_name(session: Session | None) -> str:
    """Something a human would call this session out loud."""
    if session is None:
        return "an unknown session"
    for candidate in (session.title, session.project.rsplit("/", 1)[-1]):
        if candidate:
            return candidate
    return f"the {session.adapter} session"


def _spoken_list(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


class CommandHandlers:
    """One method per intent; every path ends in a template key."""

    def __init__(
        self,
        cache: LocalCache,
        client: ServerClient,
        composer: ResponseComposer,
        *,
        overnight_hours: float = 12.0,
    ) -> None:
        self._cache = cache
        self._client = client
        self._composer = composer
        self._overnight_hours = overnight_hours

    async def handle(self, intent: Intent) -> str:
        """Execute one intent; failures become the ``error`` template."""
        try:
            return await self._dispatch(intent)
        except MjolnirError as exc:
            _log.warning("handlers.command_failed", intent=type(intent).__name__, error=str(exc))
            return self._composer.compose("error", error=str(exc))

    async def _dispatch(self, intent: Intent) -> str:
        match intent:
            case StatusIntent():
                return self._status()
            case OvernightIntent():
                return await self._overnight()
            case PendingIntent():
                return self._pending()
            case ApproveIntent(target=target):
                return await self._resolve(target, decision="allow", key="approved")
            case DenyIntent(target=target):
                return await self._resolve(target, decision="deny", key="denied")
            case StopIntent(target=target):
                return await self._stop(target)
            case HelpIntent():
                return self._composer.compose("help")
            case CancelIntent():
                return self._composer.compose("cancelled")
            case UnknownIntent(text=text):
                return self._composer.compose("unknown", text=text or "nothing")
        return self._composer.compose("unknown", text="nothing")  # pragma: no cover

    # -------------------------------------------------------------- queries

    def _status(self) -> str:
        active = self._cache.active_sessions()
        pending = self._cache.pending_interactions()
        if active:
            names = _spoken_list([speakable_name(s) for s in active])
            text = self._composer.compose("status_active", count=len(active), sessions=names)
        else:
            text = self._composer.compose("status_none")
        if pending:
            text += " " + self._composer.compose("status_pending", count=len(pending))
        return text

    async def _overnight(self) -> str:
        cutoff = datetime.now(UTC) - timedelta(hours=self._overnight_hours)
        completed: list[Session] = []
        failed: list[Session] = []
        touched = 0
        for session in self._cache.sessions():
            if session.last_activity_at < cutoff:
                continue
            touched += 1
            if session.state == SessionState.COMPLETED:
                completed.append(session)
            elif session.state == SessionState.FAILED:
                failed.append(session)
        blocked = self._cache.pending_interactions()
        if touched == 0 and not blocked:
            return self._composer.compose("overnight_none", hours=int(self._overnight_hours))

        parts = [self._composer.compose("overnight_intro", count=max(touched, len(blocked)))]
        parts.extend(
            self._composer.compose("overnight_completed", name=speakable_name(s)) for s in completed
        )
        parts.extend(
            self._composer.compose(
                "overnight_failed",
                name=speakable_name(s),
                reason=f" with {s.metadata['reason']}" if s.metadata.get("reason") else "",
            )
            for s in failed
        )
        parts.extend(
            self._composer.compose(
                "overnight_blocked",
                name=speakable_name(self._cache.session(i.session_id)),
                title=i.title,
            )
            for i in blocked
        )
        # The briefing is the one response allowed through the optional LLM
        # persona layer; on any failure the deterministic text is spoken.
        return await self._composer.rephrase(" ".join(parts))

    def _pending(self) -> str:
        pending = self._cache.pending_interactions()
        if not pending:
            return self._composer.compose("pending_none")
        first = pending[0]
        key = "pending_one" if len(pending) == 1 else "pending_many"
        return self._composer.compose(
            key,
            count=len(pending),
            adapter=first.adapter,
            name=speakable_name(self._cache.session(first.session_id)),
            title=first.title,
        )

    # ------------------------------------------------------------- commands

    async def _resolve(self, target: str, *, decision: str, key: str) -> str:
        pending = self._cache.pending_interactions()
        if not pending:
            return self._composer.compose("pending_none")
        matches = self._match_interactions(pending, target) if target else pending
        if not matches:
            return self._composer.compose("not_found", query=target)
        if len(matches) > 1:
            if target:
                return self._composer.compose("ambiguous", count=len(matches), query=target)
            return self._pending()  # "approve it" with several pending: say what's first
        interaction = matches[0]
        allow: bool = decision == "allow"
        try:
            await self._client.answer(interaction.id, decision="allow" if allow else "deny")
        except AlreadyResolvedError:
            return self._composer.compose("already_resolved")
        return self._composer.compose(key)

    async def _stop(self, target: str) -> str:
        active = self._cache.active_sessions()
        matches = self._match_sessions(active, target) if target else active
        if not matches:
            return self._composer.compose("not_found", query=target or "an active session")
        if len(matches) > 1:
            return self._composer.compose(
                "ambiguous", count=len(matches), query=target or "active sessions"
            )
        session = matches[0]
        await self._client.terminate(session.id)
        return self._composer.compose("stopped", name=speakable_name(session))

    # ------------------------------------------------------------- matching

    def _match_interactions(self, pending: list[Interaction], target: str) -> list[Interaction]:
        needle = normalize(target)
        out: list[Interaction] = []
        for interaction in pending:
            session = self._cache.session(interaction.session_id)
            haystacks = [interaction.title, interaction.adapter, speakable_name(session)]
            if session is not None:
                haystacks += [session.project, session.title]
            if self._hit(needle, haystacks):
                out.append(interaction)
        return out

    def _match_sessions(self, sessions: list[Session], target: str) -> list[Session]:
        needle = normalize(target)
        return [
            s
            for s in sessions
            if self._hit(needle, [speakable_name(s), s.project, s.title, s.adapter])
        ]

    @staticmethod
    def _hit(needle: str, haystacks: list[str]) -> bool:
        return any(_match_norm(needle) in _match_norm(h) for h in haystacks if h)


def _match_norm(text: str) -> str:
    """Separator-insensitive matching: spoken "api tests" hits "api-tests"."""
    return normalize(re.sub(r"[-_/.]", " ", text))
