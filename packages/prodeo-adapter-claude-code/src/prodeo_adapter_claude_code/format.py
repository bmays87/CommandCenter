"""Shared human-facing formatting for Claude Code permission requests.

Both delivery paths — the SDK ``can_use_tool`` bridge and the interactive
``PermissionRequest`` hook (ADR-0011) — must present a permission identically,
so the dashboard card and the voice readout do not depend on how the session
was started.

``AskUserQuestion`` is the special case (ADR-0019): it is not a permission to
grant but a question to *answer*, so it becomes a ``question``-kind interaction
carrying the option labels, and the chosen label maps back to the
``updatedInput`` shape the agent expects. Everything Claude-Code-specific about
that mapping lives here, in the adapter — core stays generic.
"""

import json
import re
from typing import Any

#: Tool input shown to the human, capped so one giant argument blob cannot
#: bloat an interaction card (the full input still reaches the agent).
INTERACTION_BODY_CHARS = 4000

#: The tool Claude Code uses to ask the user a multiple-choice question.
QUESTION_TOOL = "AskUserQuestion"

#: "option 2" / "number 2" / "2" — how a human names a choice by position.
_ORDINAL_CHOICE = re.compile(r"^(?:option\s+|number\s+)?(\d+)\.?$", re.IGNORECASE)


def permission_prompt(tool_name: str, input_data: dict[str, Any]) -> tuple[str, str]:
    """(title, body) for a permission request on ``tool_name``."""
    body = json.dumps(input_data, indent=2, default=str)[:INTERACTION_BODY_CHARS]
    return f"Allow {tool_name}?", body


def interaction_content(
    tool_name: str, input_data: dict[str, Any]
) -> tuple[str, str, str, list[str]]:
    """(kind, title, body, options) for a tool call awaiting a human.

    A single-select, single-question ``AskUserQuestion`` becomes a
    ``question``-kind interaction: the title is the question itself, the body
    lists every option with its description (written to be read aloud as much
    as rendered), and ``options`` carries the labels for one-click answering.
    Everything else — other tools, multi-question or multi-select calls (a v1
    limitation, ADR-0019) — keeps the permission presentation.
    """
    question = _single_question(input_data) if tool_name == QUESTION_TOOL else None
    if question is None:
        title, body = permission_prompt(tool_name, input_data)
        return "permission", title, body, []
    text = str(question.get("question", "")).strip()
    labels = _labels(question)
    lines = [text, ""]
    for index, option in enumerate(question["options"], start=1):
        label = str(option.get("label", "")).strip()
        description = str(option.get("description", "")).strip()
        lines.append(f"{index}. {label}" + (f" — {description}" if description else ""))
    return "question", text, "\n".join(lines)[:INTERACTION_BODY_CHARS], labels


def question_updated_input(input_data: dict[str, Any], chosen: str) -> dict[str, Any] | None:
    """The ``updatedInput`` that answers an ``AskUserQuestion`` with ``chosen``.

    ``chosen`` is whatever the human supplied — a clicked option label, typed
    text, or a spoken "option two". Matching is exact label first, then
    case-insensitive, then by position. ``None`` when nothing matches: the
    caller must not fabricate a choice the human did not make.

    The contract: the agent reads the selection from ``answers``, a map of
    question text to the chosen option label, alongside the original input.
    """
    question = _single_question(input_data)
    if question is None:
        return None
    label = _match_label(_labels(question), chosen)
    if label is None:
        return None
    return {**input_data, "answers": {str(question.get("question", "")): label}}


def _single_question(input_data: dict[str, Any]) -> dict[str, Any] | None:
    """The one single-select question in ``input_data``, if that is its shape."""
    questions = input_data.get("questions")
    if not isinstance(questions, list) or len(questions) != 1:
        return None
    question = questions[0]
    if not isinstance(question, dict) or question.get("multiSelect"):
        return None
    options = question.get("options")
    if not isinstance(options, list) or not options:
        return None
    if not all(isinstance(o, dict) and str(o.get("label", "")).strip() for o in options):
        return None
    return question


def _labels(question: dict[str, Any]) -> list[str]:
    return [str(option.get("label", "")).strip() for option in question["options"]]


def _match_label(labels: list[str], chosen: str) -> str | None:
    chosen = chosen.strip()
    if not chosen:
        return None
    if chosen in labels:
        return chosen
    lowered = [label.casefold() for label in labels]
    if chosen.casefold() in lowered:
        return labels[lowered.index(chosen.casefold())]
    ordinal = _ORDINAL_CHOICE.match(chosen)
    if ordinal is not None:
        position = int(ordinal.group(1))
        if 1 <= position <= len(labels):
            return labels[position - 1]
    return None
