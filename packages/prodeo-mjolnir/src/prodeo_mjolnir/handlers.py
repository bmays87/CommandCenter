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
    RespondIntent,
    StatusIntent,
    StopIntent,
    UnknownIntent,
    normalize,
)

_log = structlog.get_logger(__name__)

#: Positions read out loud, indexed 1..N (index 0 unused).
_ORDINAL_NAMES = (
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
)

#: A ``#N`` positional target produced by the ordinal grammar.
_POSITIONAL = re.compile(r"#(\d+)")


def _ordinal_name(index: int) -> str:
    """ "One", "Two", ... or "Number 11" past the spelled-out range."""
    return _ORDINAL_NAMES[index] if index < len(_ORDINAL_NAMES) else f"Number {index}"


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
        #: The pending interactions as last read out, so "approve number two"
        #: resolves against exactly what the user heard (not a since-changed list).
        self._last_pending: list[Interaction] = []

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
            case RespondIntent(target=target, text=text):
                return await self._respond(target, text)
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
        # Remember the announced ordering so a positional "approve number two"
        # (or a later "respond to one ...") resolves against what was read out.
        self._last_pending = list(pending)
        if not pending:
            return self._composer.compose("pending_none")
        if len(pending) == 1:
            first = pending[0]
            return self._composer.compose(
                "pending_one",
                count=1,
                adapter=first.adapter,
                name=speakable_name(self._cache.session(first.session_id)),
                title=first.title,
            )
        items = " ".join(
            f"{_ordinal_name(idx)}: {i.adapter} on "
            f"{speakable_name(self._cache.session(i.session_id))} asks: {i.title}."
            for idx, i in enumerate(pending, start=1)
        )
        return self._composer.compose("pending_list", count=len(pending), items=items)

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

    async def _respond(self, target: str, text: str) -> str:
        """Free-text answer to a question-kind interaction (RespondIntent)."""
        pending = self._cache.pending_interactions()
        if not pending:
            return self._composer.compose("pending_none")
        matches = self._match_interactions(pending, target) if target else pending
        if not matches:
            return self._composer.compose("not_found", query=target)
        if len(matches) > 1:
            if target:
                return self._composer.compose("ambiguous", count=len(matches), query=target)
            return self._pending()  # "respond ..." with several pending and no target
        interaction = matches[0]
        try:
            await self._client.answer(interaction.id, text=text)
        except AlreadyResolvedError:
            return self._composer.compose("already_resolved")
        return self._composer.compose("responded")

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
        position = self._positional(target)
        if position is not None:
            # Resolve "#N" against exactly what was read out; if nothing has
            # been announced yet, fall back to the current sorted pending.
            source = self._last_pending or pending
            live = {i.id for i in pending}
            if 1 <= position <= len(source) and source[position - 1].id in live:
                return [source[position - 1]]
            return []
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
    def _positional(target: str) -> int | None:
        """The 1-based position in a ``#N`` positional target, else None."""
        match = _POSITIONAL.fullmatch(target.strip())
        return int(match.group(1)) if match else None

    @staticmethod
    def _hit(needle: str, haystacks: list[str]) -> bool:
        return any(_match_norm(needle) in _match_norm(h) for h in haystacks if h)


def _match_norm(text: str) -> str:
    """Separator-insensitive matching: spoken "api tests" hits "api-tests"."""
    return normalize(re.sub(r"[-_/.]", " ", text))
