"""Deterministic intent routing (v1).

Pattern/grammar based on purpose: low latency, predictable behavior, fully
offline (voice-pipeline.md). An optional LLM router (``llm_router.py``) layers
*on top* of this baseline via the :class:`Router` Protocol - it is a fallback
for phrasings the grammar misses, never a replacement. Patterns are ordered -
first match wins - and matched against normalized text (lowercased, punctuation
stripped).
"""

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class StatusIntent:
    """ "How are things?" - active sessions and anything pending."""


@dataclass(frozen=True)
class OvernightIntent:
    """ "What happened overnight?" - the morning briefing."""


@dataclass(frozen=True)
class PendingIntent:
    """ "Anything need me?" - pending interactions only."""


@dataclass(frozen=True)
class ApproveIntent:
    """Approve a pending permission; ``target`` narrows which one."""

    target: str = ""


@dataclass(frozen=True)
class DenyIntent:
    """Deny a pending permission; ``target`` narrows which one."""

    target: str = ""


@dataclass(frozen=True)
class RespondIntent:
    """Free-text answer to a question-kind interaction named by ``target``."""

    target: str = ""
    text: str = ""


@dataclass(frozen=True)
class StopIntent:
    """Terminate a session named by ``target``."""

    target: str = ""


@dataclass(frozen=True)
class HelpIntent:
    """ "What can you do?"."""


@dataclass(frozen=True)
class CancelIntent:
    """ "Never mind." - end the exchange without acting."""


@dataclass(frozen=True)
class UnknownIntent:
    """Nothing matched; carries the transcript for the fallback response."""

    text: str = ""


Intent = (
    StatusIntent
    | OvernightIntent
    | PendingIntent
    | ApproveIntent
    | DenyIntent
    | RespondIntent
    | StopIntent
    | HelpIntent
    | CancelIntent
    | UnknownIntent
)


@runtime_checkable
class Router(Protocol):
    """Maps one transcript to one :class:`Intent`. The deterministic
    :class:`IntentRouter` and the optional ``LlmIntentRouter`` both satisfy it,
    so the pipeline depends on the seam, not the implementation."""

    async def route(self, text: str) -> Intent: ...


#: Filler the grammar tolerates around a session/interaction reference.
_ARTICLES = re.compile(r"^(?:the|that|this|my)\s+|\s+(?:session|agent|one)$")

_TARGET = r"(?:\s+(?:for|on|of))?\s+(?P<target>.+?)"

#: Spoken/typed ordinal -> position. Both the word and the digit are accepted.
_ORDINAL_TO_INT: dict[str, int] = {
    "one": 1, "1": 1, "first": 1,
    "two": 2, "2": 2, "second": 2,
    "three": 3, "3": 3, "third": 3,
    "four": 4, "4": 4, "fourth": 4,
    "five": 5, "5": 5, "fifth": 5,
    "six": 6, "6": 6, "sixth": 6,
    "seven": 7, "7": 7, "seventh": 7,
    "eight": 8, "8": 8, "eighth": 8,
    "nine": 9, "9": 9, "ninth": 9,
    "ten": 10, "10": 10, "tenth": 10,
}  # fmt: skip
_ORD = rf"(?P<ord>{'|'.join(_ORDINAL_TO_INT)})"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # morning briefing
    (re.compile(r"^good morning\b.*"), "overnight"),
    (re.compile(r".*\bwhat happened (?:overnight|last night|while i was \w+)\b.*"), "overnight"),
    (re.compile(r".*\b(?:overnight|morning) (?:report|briefing|summary|update)\b.*"), "overnight"),
    # status
    (re.compile(r"^(?:whats the |give me a |give me the )?status(?: report| update)?$"), "status"),
    (re.compile(r"^how(?: are|s) (?:things|my agents|the agents)(?: doing| going)?$"), "status"),
    (re.compile(r"^whats? (?:is )?(?:going on|happening|running)$"), "status"),
    (
        re.compile(
            r".*\b(?:do i have|are there|have i got|any) (?:any )?(?:running|active|open) "
            r"(?:sessions|agents)\b.*"
        ),
        "status",
    ),
    (re.compile(r".*\bwhich (?:sessions|agents) are (?:running|active|open)\b.*"), "status"),
    # respond with free text (before pending/approve so "respond to two ..."
    # is a full answer, not an approve). Ordinal form first, then by name.
    (
        re.compile(
            rf"^(?:respond|reply|answer)\s+to\s+(?:number\s+|the\s+)?{_ORD}"
            r"\s+(?:with|saying)\s+(?P<text>.+)$"
        ),
        "respond",
    ),
    (
        re.compile(
            r"^(?:respond|reply|answer)\s+to\s+(?P<target>.+?)"
            r"\s+(?:with|saying)\s+(?P<text>.+)$"
        ),
        "respond",
    ),
    (
        re.compile(rf"^tell\s+(?:number\s+|the\s+)?{_ORD}\s+(?P<text>.+)$"),
        "respond",
    ),
    # pending interactions
    (re.compile(r".*\b(?:any(?:thing)?|what) (?:questions|permissions|requests)\b.*"), "pending"),
    (
        re.compile(r".*\b(?:anything|what) (?:needs?|waiting (?:on|for)) (?:me|my attention)\b.*"),
        "pending",
    ),
    (
        re.compile(
            r".*\b(?:any|are there any|are there) "
            r"(?:dialogs?|prompts?|questions?|permissions?) (?:waiting|pending)\b.*"
        ),
        "pending",
    ),
    (
        re.compile(r".*\bwaiting (?:for|on) (?:a |an )?(?:responses?|answers?|replies|reply)\b.*"),
        "pending",
    ),
    (re.compile(r".*\bwhats waiting (?:on|for) me\b.*"), "pending"),
    (re.compile(r"^any(?:thing)? pending$"), "pending"),
    # approve / deny by position ("approve number two", "deny the first one")
    (
        re.compile(
            rf"^(?:yes,?\s+)?(?:approve|allow|grant|permit)\s+"
            rf"(?:number\s+|the\s+)?{_ORD}(?:\s+one)?$"
        ),
        "approve",
    ),
    (
        re.compile(
            rf"^(?:no,?\s+)?(?:deny|reject|refuse|decline|block)\s+"
            rf"(?:number\s+|the\s+)?{_ORD}(?:\s+one)?$"
        ),
        "deny",
    ),
    # approve / deny (optionally targeted by name)
    (
        re.compile(
            r"^(?:yes,?\s+)?(?:approve|allow|grant|permit)"
            r"(?:\s+(?:it|that|them|the (?:permission|request)))?"
            rf"(?:{_TARGET})?$"
        ),
        "approve",
    ),
    (
        re.compile(
            r"^(?:no,?\s+)?(?:deny|reject|refuse|decline|block)"
            r"(?:\s+(?:it|that|them|the (?:permission|request)))?"
            rf"(?:{_TARGET})?$"
        ),
        "deny",
    ),
    # stop a session
    (
        re.compile(rf"^(?:stop|terminate|kill|shut down)(?:\s+(?:it|that))?(?:{_TARGET})?$"),
        "stop",
    ),
    # meta
    (re.compile(r"^(?:help|what can (?:you do|i say)|what do you do)$"), "help"),
    (re.compile(r"^(?:never\s?mind|cancel|forget it|nothing|no thanks?)$"), "cancel"),
]


def normalize(text: str) -> str:
    """Lowercase, strip punctuation (transcripts vary), collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s'-]", " ", text)
    text = text.replace("'", "")
    return re.sub(r"\s+", " ", text).strip()


def _clean_target(raw: str) -> str:
    return _ARTICLES.sub("", raw.strip()).strip()


def _target_from(match: re.Match[str]) -> str:
    """A ``#N`` positional target from an ordinal group, else the cleaned name."""
    groups = match.groupdict()
    ordinal = groups.get("ord")
    if ordinal is not None:
        return f"#{_ORDINAL_TO_INT[ordinal]}"
    return _clean_target(groups.get("target") or "")


class IntentRouter:
    """Maps one transcript to one :class:`Intent` (first pattern wins)."""

    async def route(self, text: str) -> Intent:
        normalized = normalize(text)
        if not normalized:
            return UnknownIntent(text=text)
        for pattern, name in _PATTERNS:
            match = pattern.match(normalized)
            if match is None:
                continue
            target = _target_from(match)
            if name == "overnight":
                return OvernightIntent()
            if name == "status":
                return StatusIntent()
            if name == "pending":
                return PendingIntent()
            if name == "respond":
                answer = (match.groupdict().get("text") or "").strip()
                return RespondIntent(target=target, text=answer)
            if name == "approve":
                return ApproveIntent(target=target)
            if name == "deny":
                return DenyIntent(target=target)
            if name == "stop":
                return StopIntent(target=target)
            if name == "help":
                return HelpIntent()
            return CancelIntent()
        return UnknownIntent(text=normalized)
