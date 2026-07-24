"""Persona template packs (voice-pipeline.md: persona layer 2).

Every spoken response is a template with named fields; ``{honorific}``
renders as ", <honorific>" when one is configured and as nothing otherwise.
Packs restyle phrasing only - they are deterministic text, keeping v1's
offline guarantee and latency budget untouched. A user pack file (JSON,
key -> template) is layered on top of a built-in pack, so overriding one
phrase does not mean rewriting them all.

Both built-in packs use original phrasing; persona voices/styles must be
original, stock, or licensed - never a clone of a real person.
"""

import json
from pathlib import Path

from prodeo_mjolnir.errors import MjolnirError

NEUTRAL: dict[str, str] = {
    "ack": "Yes{honorific}?",
    "not_heard": "I didn't catch that{honorific}.",
    "status_none": "No sessions are active{honorific}.",
    "status_active": "{count} session{plural} active{honorific}: {sessions}.",
    "status_pending": "{count} interaction{plural} awaiting your answer.",
    "overnight_none": "All quiet{honorific}. No agent activity in the last {hours} hours.",
    "overnight_intro": "{count} agent session{plural} ran while you were away{honorific}.",
    "overnight_completed": "{name} finished.",
    "overnight_failed": "{name} failed{reason}.",
    "overnight_blocked": "{name} is waiting on you: {title}.",
    "pending_none": "Nothing is waiting on you{honorific}.",
    "pending_one": "One thing needs you{honorific}. {adapter} on {name} asks: {title}.",
    "pending_many": "{count} things need you{honorific}. First: {adapter} on {name} asks: {title}.",
    "pending_list": "{count} things need you{honorific}. {items}",
    "approved": "Approved{honorific}.",
    "denied": "Denied{honorific}.",
    "responded": "Answered{honorific}.",
    "stopped": "{name} has been stopped{honorific}.",
    "already_resolved": "That was already answered elsewhere{honorific}.",
    "not_found": "I couldn't find anything matching {query}{honorific}.",
    "ambiguous": "{count} sessions match {query}{honorific}; say the project name.",
    "cancelled": "Very well{honorific}.",
    "unknown": "Sorry{honorific}, I didn't understand: {text}.",
    "help": (
        "You can ask for status, the overnight report, or what needs you; "
        "approve or deny a permission; or stop a session."
    ),
    "error": "Something went wrong{honorific}: {error}.",
    "notify_interaction": "{adapter} on {name} asks{honorific}: {title}.",
    "notify_completed": "{name} has completed{honorific}.",
    "notify_failed": "{name} has failed{honorific}.",
}

#: A calm, formal register in the classic AI-butler tradition (original text).
STEWARD: dict[str, str] = {
    **NEUTRAL,
    "ack": "At your service{honorific}.",
    "not_heard": "I beg your pardon{honorific}, I didn't catch that.",
    "status_none": "All is quiet{honorific}; no sessions are active.",
    "status_active": "At present{honorific}, {count} session{plural} are at work: {sessions}.",
    "status_pending": "{count} matter{plural} await your decision.",
    "overnight_none": (
        "A quiet night{honorific}; the agents had nothing to report in the last {hours} hours."
    ),
    "overnight_intro": "While you slept{honorific}, {count} session{plural} were at work.",
    "overnight_completed": "{name} concluded successfully.",
    "overnight_failed": "{name}, regrettably, failed{reason}.",
    "overnight_blocked": "{name} awaits your word: {title}.",
    "pending_none": "Nothing requires your attention{honorific}.",
    "pending_one": "One matter requires you{honorific}. {adapter} on {name} asks: {title}.",
    "pending_many": (
        "{count} matters require you{honorific}. The first: {adapter} on {name} asks: {title}."
    ),
    "pending_list": "{count} matters require you{honorific}. {items}",
    "approved": "As you wish{honorific}. The permission has been granted.",
    "denied": "As you wish{honorific}. The request has been declined.",
    "responded": "As you wish{honorific}. Your reply has been sent.",
    "stopped": "As you wish{honorific}. {name} has been terminated.",
    "already_resolved": "It appears someone attended to that already{honorific}.",
    "cancelled": "Of course{honorific}.",
    "unknown": "My apologies{honorific}, I'm not sure what you meant by: {text}.",
    "error": "My apologies{honorific}; something has gone wrong: {error}.",
    "notify_interaction": "Pardon the interruption{honorific}. {adapter} on {name} asks: {title}.",
    "notify_completed": "{name} has concluded{honorific}.",
    "notify_failed": "I'm afraid {name} has failed{honorific}.",
}

BUILTIN_PACKS: dict[str, dict[str, str]] = {"neutral": NEUTRAL, "steward": STEWARD}


def load_pack(name: str, pack_file: Path | None = None) -> dict[str, str]:
    """A built-in pack, optionally overlaid with a user JSON pack file."""
    base = BUILTIN_PACKS.get(name)
    if base is None:
        raise MjolnirError(
            f"unknown persona pack {name!r} (built-in packs: {', '.join(sorted(BUILTIN_PACKS))})"
        )
    pack = dict(base)
    if pack_file is not None:
        overrides = json.loads(pack_file.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            raise MjolnirError(f"persona pack file {pack_file} must be a JSON object")
        unknown = set(overrides) - set(NEUTRAL)
        if unknown:
            raise MjolnirError(f"persona pack file overrides unknown keys: {sorted(unknown)}")
        pack.update({str(k): str(v) for k, v in overrides.items()})
    return pack
