"""Shared human-facing formatting for Claude Code permission requests.

Both delivery paths — the SDK ``can_use_tool`` bridge and the interactive
``PermissionRequest`` hook (ADR-0011) — must present a permission identically,
so the dashboard card and the voice readout do not depend on how the session
was started.

``AskUserQuestion`` is the special case (ADR-0019, ADR-0022): it is not a
permission to grant but a question to *answer*, so it becomes a
``question``-kind interaction carrying structured :class:`QuestionGroup`s
(multi-question and multi-select included), and the human's selections map
back to the ``updatedInput`` shape the agent expects. Everything
Claude-Code-specific about that mapping lives here, in the adapter — core
stays generic.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from prodeo.mediation.model import QuestionGroup, QuestionOption

#: Tool input shown to the human, capped so one giant argument blob cannot
#: bloat an interaction card (the full input still reaches the agent).
INTERACTION_BODY_CHARS = 4000

#: The tool Claude Code uses to ask the user a multiple-choice question.
QUESTION_TOOL = "AskUserQuestion"

#: "option 2" / "number 2" / "2" — how a human names a choice by position.
_ORDINAL_CHOICE = re.compile(r"^(?:option\s+|number\s+)?(\d+)\.?$", re.IGNORECASE)

#: How Claude Code's ``answers`` map encodes a multi-select choice: the chosen
#: labels joined with ", ". Verified against real multiSelect transcripts
#: (e.g. ``"answers": {"...same area?": "Preflight check in start-mjolnir.ps1,
#: Fix --all-packages doc"}``); this contract is Claude-Code-owned and this
#: module is its single follower (ADR-0019 Consequences, ADR-0022).
_MULTI_SELECT_JOIN = ", "


@dataclass(frozen=True)
class InteractionContent:
    """How one blocked tool call is presented to a human."""

    kind: str
    title: str
    body: str
    #: Flat labels for one-click/voice ordinal answering — populated only for
    #: the single-question single-select shape (the only one a bare text
    #: answer can address).
    options: list[str] = field(default_factory=list)
    #: The structured questions (question kind only; empty for permissions).
    questions: list[QuestionGroup] = field(default_factory=list)


def permission_prompt(tool_name: str, input_data: dict[str, Any]) -> tuple[str, str]:
    """(title, body) for a permission request on ``tool_name``."""
    body = json.dumps(input_data, indent=2, default=str)[:INTERACTION_BODY_CHARS]
    return f"Allow {tool_name}?", body


def interaction_content(tool_name: str, input_data: dict[str, Any]) -> InteractionContent:
    """The presentation of a tool call awaiting a human.

    Any well-formed ``AskUserQuestion`` — one or many questions, single- or
    multi-select — becomes a ``question``-kind interaction carrying its
    structured groups; the body lists every question with numbered options
    (written to be read aloud as much as rendered). Only genuinely malformed
    input (and every other tool) keeps the permission presentation.
    """
    questions = question_groups(input_data) if tool_name == QUESTION_TOOL else []
    if not questions:
        title, body = permission_prompt(tool_name, input_data)
        return InteractionContent(kind="permission", title=title, body=body)
    title = questions[0].prompt
    if len(questions) > 1:
        title = f"{title} (+{len(questions) - 1} more)"
    lines: list[str] = []
    for group in questions:
        if lines:
            lines.append("")
        lines.append(group.prompt + (" (choose all that apply)" if group.multi_select else ""))
        lines.append("")
        for index, option in enumerate(group.options, start=1):
            suffix = f" — {option.description}" if option.description else ""
            lines.append(f"{index}. {option.label}{suffix}")
    single_single = len(questions) == 1 and not questions[0].multi_select
    return InteractionContent(
        kind="question",
        title=title,
        body="\n".join(lines)[:INTERACTION_BODY_CHARS],
        options=[o.label for o in questions[0].options] if single_single else [],
        questions=questions,
    )


def question_groups(input_data: dict[str, Any]) -> list[QuestionGroup]:
    """The structured questions in ``input_data``; empty when malformed.

    Group ids are the exact question texts (that is the key Claude Code's
    ``answers`` map uses, ADR-0019 §2); duplicate texts get ``" #2"``,
    ``" #3"`` suffixes *on the id only* — the un-suffixed text is recovered
    by index when mapping selections back.
    """
    dicts = _question_dicts(input_data)
    if dicts is None:
        return []
    seen: dict[str, int] = {}
    groups: list[QuestionGroup] = []
    for question in dicts:
        text = str(question.get("question", "")).strip()
        seen[text] = seen.get(text, 0) + 1
        groups.append(
            QuestionGroup(
                id=text if seen[text] == 1 else f"{text} #{seen[text]}",
                prompt=text,
                options=[
                    QuestionOption(
                        label=str(o.get("label", "")).strip(),
                        description=str(o.get("description", "")).strip(),
                    )
                    for o in question["options"]
                ],
                multi_select=bool(question.get("multiSelect")),
            )
        )
    return groups


def questions_updated_input(
    input_data: dict[str, Any], selections: dict[str, list[str]]
) -> dict[str, Any] | None:
    """The ``updatedInput`` answering an ``AskUserQuestion`` with ``selections``.

    ``selections`` maps group ids to chosen labels (clicked, typed, or spoken
    — matching is exact label first, then case-insensitive, then ordinal).
    Every question must end up with at least one matched label, a
    single-select question with exactly one; anything else returns ``None``:
    the caller must not fabricate a choice the human did not make.

    The contract: the agent reads selections from ``answers``, a map of
    question text to the chosen label — for multi-select, the chosen labels
    joined with ``", "`` (verified against real transcripts) — alongside the
    original input.
    """
    groups = question_groups(input_data)
    if not groups:
        return None
    raw_questions = input_data["questions"]
    answers: dict[str, str] = {}
    for index, group in enumerate(groups):
        labels = [option.label for option in group.options]
        matched: list[str] = []
        for choice in selections.get(group.id) or []:
            label = _match_label(labels, str(choice))
            if label is None:
                return None
            if label not in matched:
                matched.append(label)
        if not matched or (not group.multi_select and len(matched) > 1):
            return None
        text = str(raw_questions[index].get("question", "")).strip()  # un-suffixed
        answers[text] = _MULTI_SELECT_JOIN.join(matched)
    return {**input_data, "answers": answers}


def question_updated_input(input_data: dict[str, Any], chosen: str) -> dict[str, Any] | None:
    """The ``updatedInput`` that answers an ``AskUserQuestion`` with ``chosen``.

    The bare-text path: only the single-question single-select shape is
    answerable this way (one string cannot address several questions or a
    multi-selection). ``chosen`` is whatever the human supplied — a clicked
    option label, typed text, or a spoken "option two". ``None`` when nothing
    matches: the caller must not fabricate a choice the human did not make.
    """
    question = _single_question(input_data)
    if question is None:
        return None
    label = _match_label(_labels(question), chosen)
    if label is None:
        return None
    return {**input_data, "answers": {str(question.get("question", "")): label}}


def _question_dicts(input_data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The well-formed question dicts in ``input_data``, or None."""
    questions = input_data.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    validated: list[dict[str, Any]] = []
    for question in questions:
        if not isinstance(question, dict):
            return None
        if not str(question.get("question", "")).strip():
            return None
        options = question.get("options")
        if not isinstance(options, list) or not options:
            return None
        if not all(isinstance(o, dict) and str(o.get("label", "")).strip() for o in options):
            return None
        validated.append(question)
    return validated


def _single_question(input_data: dict[str, Any]) -> dict[str, Any] | None:
    """The one single-select question in ``input_data``, if that is its shape."""
    dicts = _question_dicts(input_data)
    if dicts is None or len(dicts) != 1 or dicts[0].get("multiSelect"):
        return None
    return dicts[0]


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
