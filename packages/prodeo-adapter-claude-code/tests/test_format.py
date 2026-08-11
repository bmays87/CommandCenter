"""AskUserQuestion presentation and answer mapping (ADR-0019).

The contract under test: a single-select question becomes a question-kind
interaction whose body reads aloud sensibly, and a human's reply - clicked
label, typed text, or "option 2" - maps back to the ``updatedInput`` shape the
agent expects, or to nothing at all rather than a fabricated choice.
"""

from prodeo_adapter_claude_code.format import (
    interaction_content,
    permission_prompt,
    question_updated_input,
)


def question_input(**question_overrides: object) -> dict[str, object]:
    question: dict[str, object] = {
        "question": "Which approach should we take?",
        "header": "Approach",
        "multiSelect": False,
        "options": [
            {"label": "Option 1 (Recommended)", "description": "The safe path"},
            {"label": "Option 2", "description": "The fast path"},
        ],
    }
    question.update(question_overrides)
    return {"questions": [question]}


def test_single_question_becomes_a_question_interaction() -> None:
    kind, title, body, options = interaction_content("AskUserQuestion", question_input())

    assert kind == "question"
    assert title == "Which approach should we take?"
    # The body is the full readout: question, then each option with its
    # description - what the dashboard shows and what voice will speak.
    assert body.splitlines()[0] == "Which approach should we take?"
    assert "1. Option 1 (Recommended) — The safe path" in body
    assert "2. Option 2 — The fast path" in body
    assert options == ["Option 1 (Recommended)", "Option 2"]


def test_other_tools_keep_the_permission_presentation() -> None:
    input_data = {"command": "rm -rf build"}

    kind, title, body, options = interaction_content("Bash", input_data)

    assert (kind, options) == ("permission", [])
    assert (title, body) == permission_prompt("Bash", input_data)


def test_multi_question_and_multiselect_fall_back_to_permission() -> None:
    # One option row cannot answer two questions, and a single label cannot
    # express a multi-selection - v1 keeps allow/deny for both (ADR-0019).
    two = {"questions": [question_input()["questions"][0]] * 2}  # type: ignore[index]
    multi = question_input(multiSelect=True)

    assert interaction_content("AskUserQuestion", two)[0] == "permission"
    assert interaction_content("AskUserQuestion", multi)[0] == "permission"


def test_chosen_label_maps_to_updated_input_answers() -> None:
    updated = question_updated_input(question_input(), "Option 2")

    assert updated is not None
    assert updated["answers"] == {"Which approach should we take?": "Option 2"}
    # The original input survives alongside the answers.
    assert updated["questions"] == question_input()["questions"]


def test_matching_is_case_insensitive_then_positional() -> None:
    by_case = question_updated_input(question_input(), "option 1 (recommended)")
    assert by_case is not None
    assert by_case["answers"]["Which approach should we take?"] == "Option 1 (Recommended)"

    for spoken in ("option 2", "number 2", "2", "2."):
        by_position = question_updated_input(question_input(), spoken)
        assert by_position is not None, spoken
        assert by_position["answers"]["Which approach should we take?"] == "Option 2"


def test_unmatched_text_maps_to_nothing() -> None:
    # "option 9", prose, or empty text must not fabricate a selection.
    assert question_updated_input(question_input(), "option 9") is None
    assert question_updated_input(question_input(), "do whatever seems best") is None
    assert question_updated_input(question_input(), "  ") is None


def test_malformed_question_input_maps_to_nothing() -> None:
    assert question_updated_input({}, "Option 1") is None
    assert question_updated_input({"questions": "not a list"}, "Option 1") is None
    assert question_updated_input(question_input(options=[]), "Option 1") is None
