"""Deterministic intent routing (v1).

Pattern/grammar based on purpose: low latency, predictable behavior, fully
offline (voice-pipeline.md). An LLM router would be a plugin upgrade, not a
replacement for this baseline. Patterns are ordered - first match wins - and
matched against normalized text (lowercased, punctuation stripped).
"""

import re
from dataclasses import dataclass


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
    | StopIntent
    | HelpIntent
    | CancelIntent
    | UnknownIntent
)

#: Filler the grammar tolerates around a session/interaction reference.
_ARTICLES = re.compile(r"^(?:the|that|this|my)\s+|\s+(?:session|agent|one)$")

_TARGET = r"(?:\s+(?:for|on|of))?\s+(?P<target>.+?)"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # morning briefing
    (re.compile(r"^good morning\b.*"), "overnight"),
    (re.compile(r".*\bwhat happened (?:overnight|last night|while i was \w+)\b.*"), "overnight"),
    (re.compile(r".*\b(?:overnight|morning) (?:report|briefing|summary|update)\b.*"), "overnight"),
    # status
    (re.compile(r"^(?:whats the |give me a |give me the )?status(?: report| update)?$"), "status"),
    (re.compile(r"^how(?: are|s) (?:things|my agents|the agents)(?: doing| going)?$"), "status"),
    (re.compile(r"^whats? (?:is )?(?:going on|happening|running)$"), "status"),
    # pending interactions
    (re.compile(r".*\b(?:any(?:thing)?|what) (?:questions|permissions|requests)\b.*"), "pending"),
    (
        re.compile(r".*\b(?:anything|what) (?:needs?|waiting (?:on|for)) (?:me|my attention)\b.*"),
        "pending",
    ),
    (re.compile(r"^any(?:thing)? pending$"), "pending"),
    # approve / deny (optionally targeted)
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


class IntentRouter:
    """Maps one transcript to one :class:`Intent` (first pattern wins)."""

    def route(self, text: str) -> Intent:
        normalized = normalize(text)
        if not normalized:
            return UnknownIntent(text=text)
        for pattern, name in _PATTERNS:
            match = pattern.match(normalized)
            if match is None:
                continue
            target = _clean_target(match.groupdict().get("target") or "")
            if name == "overnight":
                return OvernightIntent()
            if name == "status":
                return StatusIntent()
            if name == "pending":
                return PendingIntent()
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
