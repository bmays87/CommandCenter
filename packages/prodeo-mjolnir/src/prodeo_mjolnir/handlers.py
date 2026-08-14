"""Intent execution: cache queries + REST commands -> speakable text.

Reads come from the :class:`LocalCache` (latency budget); writes go through
the :class:`ServerClient` and the server's mediation rules apply - if a
simultaneous dashboard click wins the interaction, the voice user is told so
(``already_resolved``) rather than pretending success.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import structlog

from prodeo.mediation import Interaction, InteractionKind
from prodeo.sessions import Session, SessionState
from prodeo_mjolnir.cache import LocalCache
from prodeo_mjolnir.client import ServerClient
from prodeo_mjolnir.composer import ResponseComposer
from prodeo_mjolnir.errors import AlreadyResolvedError, MjolnirError
from prodeo_mjolnir.intents import (
    _ORDINAL_TO_INT,
    ApproveIntent,
    CancelIntent,
    DenyIntent,
    HelpIntent,
    Intent,
    LaunchIntent,
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

#: An explicit spoken yes to a confirmation (matched on normalized text).
_AFFIRMATIVE = re.compile(
    r"^(?:yes|yeah|yep|do it|go ahead|proceed|confirm|please do|sure|affirmative)\b"
)
#: An explicit spoken no.
_NEGATIVE = re.compile(r"^(?:no|nope|dont|do not|negative)\b")
#: Cancel phrases always clear a dialog (mirrors the intent grammar's cancel).
_CANCEL = re.compile(r"^(?:never\s?mind|cancel|forget it|nothing|no thanks?)$")
#: Action verbs, used to spot a reply contradicting a choose-dialog's action
#: ("deny number two" while disambiguating an approve must not approve).
_APPROVE_WORDS = re.compile(r"\b(?:approve|allow|grant|permit)\b")
_DENY_WORDS = re.compile(r"\b(?:deny|reject|refuse|decline|block)\b")

#: A choice named by position: "two", "the first one", "number two",
#: "option 2", "approve number two" - but NOT an incidental "one" inside
#: prose ("hmm the green one" must not bind position one).
_CHOICE_ORDINAL = re.compile(
    r"(?:\b(?:number|option|take|pick|choose|use|go with|approve|deny|stop)\s+(?:the\s+)?"
    rf"|^(?:the\s+)?)(?P<ord>{'|'.join(_ORDINAL_TO_INT)})(?:\s+one)?$"
)


def _spoken_position(normalized: str, upper: int) -> int | None:
    """The 1-based position named by an utterance, bounded by ``upper``."""
    match = _CHOICE_ORDINAL.search(normalized)
    if match is None:
        return None
    position = _ORDINAL_TO_INT[match.group("ord")]
    return position if 1 <= position <= upper else None


@dataclass(frozen=True)
class Reply:
    """A spoken response, plus whether Mjölnir should listen for a follow-up
    without requiring the wake word (ADR-0023)."""

    text: str
    expect_reply: bool = False


@dataclass
class _Dialog:
    """One clarification in progress; expires after the dialog TTL."""

    kind: Literal["confirm_launch", "slot_project", "slot_prompt", "choose", "question_context"]
    created: float
    data: dict[str, Any] = field(default_factory=dict)


def _project_tail(project: str) -> str:
    """The speakable tail of a project path ("CommandCenter", not "F:\\...")."""
    tail = project.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail or project


def _match_option(options: list[str], utterance: str) -> str | None:
    """Generic label matching for a spoken answer: exact (case- and
    separator-insensitive), then unique containment ("the fast path" hits
    "Fast path"), then ordinal ("option two", "the first one"). Client-side
    and agent-agnostic - the matched label is posted as plain ``text``."""
    if not options:
        return None
    needle = _match_norm(utterance)
    if not needle:
        return None
    normalized = [_match_norm(o) for o in options]
    if needle in normalized:
        return options[normalized.index(needle)]
    contained = [i for i, o in enumerate(normalized) if o in needle or needle in o]
    if len(contained) == 1:
        return options[contained[0]]
    position = _spoken_position(normalize(utterance), len(options))
    if position is not None:
        return options[position - 1]
    return None


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


class Answerer(Protocol):
    """Grounded question answering (``answers.AnswerEngine``); a Protocol so
    the dependency stays injected, matching the ``Router`` seam."""

    async def answer(self, text: str) -> str: ...


class CommandHandlers:
    """One method per intent; every path ends in a template key."""

    def __init__(
        self,
        cache: LocalCache,
        client: ServerClient,
        composer: ResponseComposer,
        *,
        overnight_hours: float = 12.0,
        answers: Answerer | None = None,
        launch_adapter: str = "",
        dialog_ttl_s: float = 90.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._cache = cache
        self._client = client
        self._composer = composer
        self._overnight_hours = overnight_hours
        self._answers = answers
        self._launch_adapter = launch_adapter
        self._dialog_ttl_s = dialog_ttl_s
        self._clock = clock or time.monotonic
        #: The pending interactions as last read out, so "approve number two"
        #: resolves against exactly what the user heard (not a since-changed list).
        self._last_pending: list[Interaction] = []
        #: The clarification in progress (confirm/slot/choose), if any. The
        #: pipeline routes follow-up utterances here before intent routing.
        self._dialog: _Dialog | None = None
        #: The interaction most recently announced out loud - an unmatched
        #: follow-up utterance may be its answer (kind "question_context").
        self._announced: _Dialog | None = None

    # -------------------------------------------------------------- dialogs

    @property
    def dialog_pending(self) -> bool:
        """True while a clarification awaits its answer (TTL-bounded)."""
        if self._dialog is None:
            return False
        if self._clock() - self._dialog.created > self._dialog_ttl_s:
            self._dialog = None
            return False
        return True

    def note_announced_interaction(self, interaction_id: str) -> None:
        """An interaction was just spoken aloud; remember it so a follow-up
        utterance can be matched against its options (ADR-0023)."""
        self._announced = _Dialog(
            kind="question_context",
            created=self._clock(),
            data={"interaction_id": interaction_id},
        )

    async def resume(self, text: str) -> Reply | None:
        """Continue the pending dialog with a follow-up utterance.

        ``None`` means "not consumed": the dialog was cleared and the caller
        should route the utterance as a fresh intent - the path a non-yes
        confirmation answer and a contradicting choose-reply take. A launch
        can only ever happen via an explicit yes inside this method.
        """
        dialog = self._dialog
        if dialog is None:
            return None
        try:
            return await self._resume(dialog, text)
        except MjolnirError as exc:
            _log.warning("handlers.dialog_failed", kind=dialog.kind, error=str(exc))
            self._dialog = None
            return Reply(self._composer.compose("error", error=str(exc)))

    async def handle(self, intent: Intent) -> Reply:
        """Execute one intent; failures become the ``error`` template."""
        try:
            return await self._dispatch(intent)
        except MjolnirError as exc:
            _log.warning("handlers.command_failed", intent=type(intent).__name__, error=str(exc))
            return Reply(self._composer.compose("error", error=str(exc)))

    async def _dispatch(self, intent: Intent) -> Reply:
        match intent:
            case StatusIntent():
                return Reply(self._status())
            case OvernightIntent():
                return Reply(await self._overnight())
            case PendingIntent():
                return Reply(self._pending())
            case ApproveIntent(target=target):
                return await self._resolve(target, decision="allow", key="approved")
            case DenyIntent(target=target):
                return await self._resolve(target, decision="deny", key="denied")
            case RespondIntent(target=target, text=text):
                return await self._respond(target, text)
            case StopIntent(target=target):
                return await self._stop(target)
            case LaunchIntent() as launch:
                return await self._launch_with(launch.project, launch.prompt)
            case HelpIntent():
                return Reply(self._composer.compose("help"))
            case CancelIntent():
                self._dialog = None
                return Reply(self._composer.compose("cancelled"))
            case UnknownIntent(text=text):
                return await self._unmatched(text)
        return Reply(self._composer.compose("unknown", text="nothing"))  # pragma: no cover

    async def _unmatched(self, text: str) -> Reply:
        """No intent matched: an announced question's answer, then a grounded
        question, then giving up.

        The announced-interaction check runs first (ADR-0023) but only binds
        when the utterance matches one of the offered options; the answer
        engine keeps its ADR-0018 place for everything else. The engine
        grounds itself in the same cache the queries read and can only
        produce speech; an empty answer - engine off, LLM down, timeout -
        falls back to the deterministic template.
        """
        announced = await self._try_announced_question(text)
        if announced is not None:
            return announced
        if self._answers is not None and text.strip():
            spoken = await self._answers.answer(text)
            if spoken:
                return Reply(spoken)
        return Reply(self._composer.compose("unknown", text=text or "nothing"))

    async def _try_announced_question(self, text: str) -> Reply | None:
        """An utterance answering the interaction that was just read out."""
        announced = self._announced
        if announced is None:
            return None
        if self._clock() - announced.created > self._dialog_ttl_s:
            self._announced = None
            return None
        interaction_id = str(announced.data["interaction_id"])
        interaction = next(
            (i for i in self._cache.pending_interactions() if i.id == interaction_id), None
        )
        if interaction is None or interaction.kind is not InteractionKind.QUESTION:
            return None
        if len(interaction.questions) > 1 or any(g.multi_select for g in interaction.questions):
            self._announced = None
            return Reply(self._composer.compose("needs_dashboard"))
        label = _match_option(interaction.options, text)
        if label is None:
            return None  # not one of the options: the answer engine's turn
        self._announced = None
        try:
            await self._client.answer(interaction.id, text=label)
        except AlreadyResolvedError:
            return Reply(self._composer.compose("already_resolved"))
        return Reply(self._composer.compose("responded"))

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

    async def _resolve(self, target: str, *, decision: str, key: str) -> Reply:
        pending = self._cache.pending_interactions()
        if not pending:
            return Reply(self._composer.compose("pending_none"))
        matches = self._match_interactions(pending, target) if target else pending
        if not matches:
            return Reply(self._composer.compose("not_found", query=target))
        if len(matches) > 1:
            if not target:
                # "approve it" with several pending: read them out and keep
                # listening - the positional follow-up needs no re-wake.
                return Reply(self._pending(), expect_reply=True)
            return self._ask_which_interaction(matches, action=("answer", decision, key))
        interaction = matches[0]
        allow: bool = decision == "allow"
        try:
            await self._client.answer(interaction.id, decision="allow" if allow else "deny")
        except AlreadyResolvedError:
            return Reply(self._composer.compose("already_resolved"))
        return Reply(self._composer.compose(key))

    async def _respond(self, target: str, text: str) -> Reply:
        """Free-text answer to a question-kind interaction (RespondIntent)."""
        pending = self._cache.pending_interactions()
        if not pending:
            return Reply(self._composer.compose("pending_none"))
        matches = self._match_interactions(pending, target) if target else pending
        if not matches:
            return Reply(self._composer.compose("not_found", query=target))
        if len(matches) > 1:
            if not target:
                return Reply(self._pending(), expect_reply=True)
            return self._ask_which_interaction(matches, action=("respond", text))
        return await self._respond_to(matches[0], text)

    async def _respond_to(self, interaction: Interaction, text: str) -> Reply:
        if len(interaction.questions) > 1 or any(g.multi_select for g in interaction.questions):
            # A single spoken answer cannot address several questions or a
            # multi-selection; free text would be refused server-side anyway.
            return Reply(self._composer.compose("needs_dashboard"))
        try:
            await self._client.answer(interaction.id, text=text)
        except AlreadyResolvedError:
            return Reply(self._composer.compose("already_resolved"))
        return Reply(self._composer.compose("responded"))

    async def _stop(self, target: str) -> Reply:
        active = self._cache.active_sessions()
        matches = self._match_sessions(active, target) if target else active
        if not matches:
            return Reply(self._composer.compose("not_found", query=target or "an active session"))
        if len(matches) > 1:
            self._dialog = self._new_dialog(
                "choose",
                {
                    "action": ("stop",),
                    "candidates": [(s.id, speakable_name(s)) for s in matches],
                    "asked_again": False,
                },
            )
            return Reply(self._choose_text([speakable_name(s) for s in matches]), expect_reply=True)
        session = matches[0]
        await self._client.terminate(session.id)
        return Reply(self._composer.compose("stopped", name=speakable_name(session)))

    def _ask_which_interaction(
        self, matches: list[Interaction], *, action: tuple[str, ...]
    ) -> Reply:
        """Clarify which of several matching interactions was meant."""
        names = [f"{speakable_name(self._cache.session(i.session_id))}: {i.title}" for i in matches]
        self._dialog = self._new_dialog(
            "choose",
            {
                "action": action,
                "candidates": [(i.id, name) for i, name in zip(matches, names, strict=True)],
                "asked_again": False,
            },
        )
        # Ordinals in the reply bind to this readout (and resume() runs before
        # the router, so "two" can never leak to _last_pending mid-dialog).
        self._last_pending = list(matches)
        return Reply(self._choose_text(names), expect_reply=True)

    # -------------------------------------------------------------- launching

    async def _launch_with(self, spoken_project: str, prompt: str) -> Reply:
        """Slot-fill a voice launch: project -> prompt -> adapter -> confirm.

        Voice launches are restricted to projects already seen in session
        history - dictating a filesystem path by voice is not viable; the
        template tells the user to start unknown projects from the dashboard
        once.
        """
        if not spoken_project:
            self._dialog = self._new_dialog("slot_project", {"prompt": prompt, "attempts": 0})
            return Reply(self._composer.compose("ask_project"), expect_reply=True)
        candidates = self._match_projects(spoken_project)
        if not candidates:
            self._dialog = self._new_dialog("slot_project", {"prompt": prompt, "attempts": 0})
            return Reply(
                self._composer.compose("unknown_project", query=spoken_project),
                expect_reply=True,
            )
        if len(candidates) > 1:
            self._dialog = self._new_dialog(
                "choose",
                {
                    "action": ("launch_project",),
                    "candidates": [(p, _project_tail(p)) for p in candidates],
                    "prompt": prompt,
                    "asked_again": False,
                },
            )
            return Reply(
                self._choose_text([_project_tail(p) for p in candidates]), expect_reply=True
            )
        return await self._launch_project_known(candidates[0], prompt)

    async def _launch_project_known(self, project: str, prompt: str) -> Reply:
        if not prompt:
            self._dialog = self._new_dialog("slot_prompt", {"project": project})
            return Reply(
                self._composer.compose("ask_prompt", project=_project_tail(project)),
                expect_reply=True,
            )
        if self._launch_adapter:
            return self._confirm_launch(self._launch_adapter, project, prompt)
        adapters = [a.name for a in await self._client.list_adapters() if a.capabilities.launch]
        if not adapters:
            return Reply(
                self._composer.compose("launch_failed", error="no agent can launch sessions")
            )
        if len(adapters) > 1:
            self._dialog = self._new_dialog(
                "choose",
                {
                    "action": ("launch_adapter",),
                    "candidates": [(a, a) for a in adapters],
                    "project": project,
                    "prompt": prompt,
                    "asked_again": False,
                },
            )
            return Reply(self._choose_text(adapters), expect_reply=True)
        return self._confirm_launch(adapters[0], project, prompt)

    def _confirm_launch(self, adapter: str, project: str, prompt: str) -> Reply:
        """The confirm-first gate: every launch path funnels through here."""
        self._dialog = self._new_dialog(
            "confirm_launch", {"adapter": adapter, "project": project, "prompt": prompt}
        )
        return Reply(
            self._composer.compose("launch_confirm", project=_project_tail(project), prompt=prompt),
            expect_reply=True,
        )

    def _match_projects(self, spoken: str) -> list[str]:
        """Projects from session history matching a spoken name.

        Compared space-stripped as well, so spoken "command center" hits a
        camel-cased "CommandCenter" directory.
        """
        projects = sorted({s.project for s in self._cache.sessions() if s.project})
        needle = _match_norm(spoken)
        compact = needle.replace(" ", "")
        if not compact:
            return []
        return [
            p
            for p in projects
            if needle in _match_norm(p) or compact in _match_norm(p).replace(" ", "")
        ]

    # ------------------------------------------------------- dialog resumption

    async def _resume(self, dialog: _Dialog, text: str) -> Reply | None:
        normalized = normalize(text)
        if _CANCEL.match(normalized):
            self._dialog = None
            return Reply(self._composer.compose("cancelled"))
        if dialog.kind == "confirm_launch":
            return await self._resume_confirm(dialog, normalized)
        if dialog.kind == "slot_project":
            return await self._resume_slot_project(dialog, text)
        if dialog.kind == "slot_prompt":
            self._dialog = None
            return await self._launch_project_known(str(dialog.data["project"]), text.strip())
        return await self._resume_choose(dialog, text)

    async def _resume_confirm(self, dialog: _Dialog, normalized: str) -> Reply | None:
        self._dialog = None  # a confirmation is one-shot, whatever the answer
        if _AFFIRMATIVE.match(normalized):
            project = str(dialog.data["project"])
            try:
                await self._client.launch(
                    adapter=str(dialog.data["adapter"]),
                    project=project,
                    prompt=str(dialog.data["prompt"]),
                )
            except MjolnirError as exc:
                return Reply(self._composer.compose("launch_failed", error=str(exc)))
            return Reply(self._composer.compose("launch_started", name=_project_tail(project)))
        if _NEGATIVE.match(normalized):
            return Reply(self._composer.compose("launch_cancelled"))
        # Anything that is not an explicit yes: never launch - route it as a
        # fresh intent instead (the caller sees None and falls through).
        return None

    async def _resume_slot_project(self, dialog: _Dialog, text: str) -> Reply | None:
        prompt = str(dialog.data["prompt"])
        candidates = self._match_projects(text)
        if len(candidates) == 1:
            self._dialog = None
            return await self._launch_project_known(candidates[0], prompt)
        if len(candidates) > 1:
            self._dialog = self._new_dialog(
                "choose",
                {
                    "action": ("launch_project",),
                    "candidates": [(p, _project_tail(p)) for p in candidates],
                    "prompt": prompt,
                    "asked_again": False,
                },
            )
            return Reply(
                self._choose_text([_project_tail(p) for p in candidates]), expect_reply=True
            )
        attempts = int(dialog.data.get("attempts", 0))
        if attempts >= 1:
            self._dialog = None
            return Reply(self._composer.compose("cancelled"))
        dialog.data["attempts"] = attempts + 1
        return Reply(
            self._composer.compose("unknown_project", query=text.strip() or "that"),
            expect_reply=True,
        )

    async def _resume_choose(self, dialog: _Dialog, text: str) -> Reply | None:
        action: tuple[str, ...] = tuple(dialog.data["action"])
        normalized = normalize(text)
        if self._contradicts(action, normalized):
            # "deny number two" while disambiguating an approve: hand the
            # utterance back to normal routing rather than mis-execute it.
            self._dialog = None
            return None
        candidates: list[tuple[str, str]] = [
            (str(c[0]), str(c[1])) for c in dialog.data["candidates"]
        ]
        chosen = self._pick_candidate(candidates, normalized)
        if chosen is None:
            if dialog.data.get("asked_again"):
                self._dialog = None
                return Reply(self._composer.compose("cancelled"))
            dialog.data["asked_again"] = True
            return Reply(self._choose_text([name for _, name in candidates]), expect_reply=True)
        self._dialog = None
        return await self._execute_choice(action, chosen, dialog.data)

    async def _execute_choice(
        self, action: tuple[str, ...], chosen: tuple[str, str], data: dict[str, Any]
    ) -> Reply:
        chosen_id, chosen_name = chosen
        if action[0] == "answer":
            decision, key = action[1], action[2]
            allow: bool = decision == "allow"
            try:
                await self._client.answer(chosen_id, decision="allow" if allow else "deny")
            except AlreadyResolvedError:
                return Reply(self._composer.compose("already_resolved"))
            return Reply(self._composer.compose(key))
        if action[0] == "respond":
            interaction = next(
                (i for i in self._cache.pending_interactions() if i.id == chosen_id), None
            )
            if interaction is None:
                return Reply(self._composer.compose("already_resolved"))
            return await self._respond_to(interaction, action[1])
        if action[0] == "stop":
            await self._client.terminate(chosen_id)
            return Reply(self._composer.compose("stopped", name=chosen_name))
        if action[0] == "launch_project":
            return await self._launch_project_known(chosen_id, str(data["prompt"]))
        # launch_adapter
        return self._confirm_launch(chosen_id, str(data["project"]), str(data["prompt"]))

    @staticmethod
    def _contradicts(action: tuple[str, ...], normalized: str) -> bool:
        if action[0] != "answer":
            return False
        if action[1] == "allow":
            return _DENY_WORDS.search(normalized) is not None
        return _APPROVE_WORDS.search(normalized) is not None

    @staticmethod
    def _pick_candidate(
        candidates: list[tuple[str, str]], normalized: str
    ) -> tuple[str, str] | None:
        """Ordinal first ("two" binds to this dialog's list, never to
        ``_last_pending``), then unique name containment either way."""
        position = _spoken_position(normalized, len(candidates))
        if position is not None:
            return candidates[position - 1]
        needle = _match_norm(normalized)
        if not needle:
            return None
        matches = [
            c for c in candidates if needle in _match_norm(c[1]) or _match_norm(c[1]) in needle
        ]
        return matches[0] if len(matches) == 1 else None

    def _choose_text(self, names: list[str]) -> str:
        items = " ".join(
            f"{_ordinal_name(index)}: {name}." for index, name in enumerate(names, start=1)
        )
        return self._composer.compose("which_one", items=items)

    def _new_dialog(
        self,
        kind: Literal["confirm_launch", "slot_project", "slot_prompt", "choose"],
        data: dict[str, Any],
    ) -> _Dialog:
        return _Dialog(kind=kind, created=self._clock(), data=data)

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
