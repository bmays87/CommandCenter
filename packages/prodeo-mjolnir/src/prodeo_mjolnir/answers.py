"""Grounded question answering: the fallback when nothing was a command.

Before this module, an utterance outside the intent grammar dead-ended in
"Sorry, I didn't understand" - Mjölnir could route commands but could not
*answer* anything, not even "what do you actually do?". This engine gives the
LLM the two things it was missing (ADR-0018):

- a **frame of reference**: who Mjölnir is, what agents, sessions, and
  permission requests are, and which spoken commands exist;
- **current state**: the live session and pending-permission picture from the
  same :class:`~prodeo_mjolnir.cache.LocalCache` every query handler reads.

It is a talker, never an actor. The safety envelope of ADR-0012 is untouched:
actions flow only through the closed intent enum and the handlers' target
resolution; this engine can produce nothing but speech, and any failure - LLM
down, timeout, empty reply - collapses to the ordinary "didn't understand"
template.
"""

from datetime import UTC, datetime

import httpx
import structlog

from prodeo.sessions import Session, SessionState
from prodeo_mjolnir.cache import LocalCache

_log = structlog.get_logger(__name__)

#: How far back "recently finished" reaches in the state snapshot.
_RECENT_HOURS = 12.0

_FRAME_OF_REFERENCE = (
    "You are Mjölnir, the spoken voice assistant of Prodeo Command Center, an "
    "operating system for AI coding agents. Your purpose is to let your user "
    "oversee their agents by voice: report what is running, brief them on what "
    "happened, and handle the permission requests agents raise.\n"
    "Domain: a coding agent is an AI programming assistant (Claude Code, Aider, "
    "and Codex are the kinds here) that works on a codebase. One working agent "
    "is a session, tied to a project. Agents pause and ask permission before "
    "risky steps - running a command, editing certain files - and those are the "
    "pending permission requests; work is blocked until someone answers. The "
    "user can also watch everything from a dashboard; you are the voice on top "
    "of the same system.\n"
    "Distinguish questions from requests for action. A question - what "
    "something is, how the system works, what is happening, what you are - "
    "you answer yourself, directly, from this briefing and the CURRENT STATE "
    "block; never redirect a question to a command. A request to *act* you "
    "cannot perform in this reply. The spoken commands that exist are: "
    "'what's the status', 'what needs me', 'what happened overnight', "
    "'approve ...' or 'deny ...' for a permission, 'respond to ... saying ...' "
    "for a question an agent asked, and 'stop ...' for a session. If a "
    "command covers the requested action, tell the user the phrase to say; "
    "for anything else, say plainly that you cannot do that.\n"
    "Grounding rules: the CURRENT STATE block is your only source for live "
    "facts. Never invent sessions, counts, names, or requests. If the answer "
    "is not in the state block or this briefing, say you do not know. You are "
    "heard, not read: answer in one to three short plain sentences, no lists, "
    "no markdown, no code."
)


def _speakable_name(session: Session) -> str:
    """Something a human would call this session out loud.

    Mirrors ``handlers.speakable_name`` for full Session objects; kept local so
    handlers can depend on this module through a Protocol without a cycle.
    """
    for candidate in (session.title, session.project.rsplit("/", 1)[-1]):
        if candidate:
            return candidate
    return f"the {session.adapter} session"


def _ago(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes ago"
    hours = round(minutes / 60)
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


class AnswerEngine:
    """One grounded, non-streaming LLM call per unrecognized utterance."""

    def __init__(
        self,
        cache: LocalCache,
        *,
        base_url: str,
        model: str,
        honorific: str = "",
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cache = cache
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._honorific = honorific
        self._timeout_s = timeout_s
        self._transport = transport

    async def answer(self, text: str) -> str:
        """A spoken answer to ``text``, or ``""`` - the caller's cue to fall
        back to the deterministic "didn't understand" template."""
        try:
            body = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": text},
                ],
                "stream": False,
            }
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post("/api/chat", json=body)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # unreachable / timeout / non-200: fail closed
            _log.warning("answers.unavailable", error=str(exc))
            return ""
        spoken = str(data.get("message", {}).get("content", "")).strip()
        _log.info("answers.answered", question=text, empty=not spoken)
        return spoken

    # ------------------------------------------------------------- context

    def _system_prompt(self) -> str:
        parts = [_FRAME_OF_REFERENCE]
        if self._honorific:
            parts.append(f"Address the user as {self._honorific}.")
        parts.append("CURRENT STATE:\n" + self._state_block())
        return "\n\n".join(parts)

    def _state_block(self) -> str:
        """The live picture, in the same order the status handlers speak it."""
        now = datetime.now(UTC)
        lines: list[str] = [f"Time: {now.astimezone().strftime('%A %H:%M')}."]

        active = self._cache.active_sessions()
        if active:
            lines.append(f"Active sessions ({len(active)}):")
            lines.extend(
                f"- {_speakable_name(s)}: {s.adapter} agent, {s.state.value}"
                + (f", project {s.project}" if s.project else "")
                + f", last active {_ago(s.last_activity_at, now)}"
                for s in active
            )
        else:
            lines.append("Active sessions: none.")

        pending = self._cache.pending_interactions()
        if pending:
            lines.append(f"Pending permission requests ({len(pending)}), oldest first:")
            lines.extend(
                f"- number {idx}: {i.adapter} on "
                f"{self._session_name(i.session_id)} asks: {i.title} "
                f"(waiting since {_ago(i.requested_at, now)})"
                for idx, i in enumerate(pending, start=1)
            )
        else:
            lines.append("Pending permission requests: none.")

        recent = [
            s
            for s in self._cache.sessions()
            if s.state in (SessionState.COMPLETED, SessionState.FAILED)
            and (now - s.last_activity_at).total_seconds() <= _RECENT_HOURS * 3600
        ]
        if recent:
            lines.append(f"Finished in the last {int(_RECENT_HOURS)} hours:")
            lines.extend(f"- {_speakable_name(s)}: {s.state.value}" for s in recent)
        return "\n".join(lines)

    def _session_name(self, session_id: str) -> str:
        session = self._cache.session(session_id)
        return _speakable_name(session) if session is not None else "an unknown session"
