"""AskUserQuestion presentation and answer mapping (ADR-0019, ADR-0022).

The contract under test: every well-formed ``AskUserQuestion`` — one or many
questions, single- or multi-select — becomes a question-kind interaction
whose body reads aloud sensibly, and a human's reply (clicked labels, typed
text, or "option 2") maps back to the ``updatedInput`` shape the agent
expects, or to nothing at all rather than a fabricated choice.
"""

from prodeo_adapter_claude_code.format import (
    interaction_content,
    permission_prompt,
    question_updated_input,
    questions_updated_input,
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


def two_question_input() -> dict[str, object]:
    return {
        "questions": [
            question_input()["questions"][0],  # type: ignore[index]
            {
                "question": "Which features do you want?",
                "header": "Features",
                "multiSelect": True,
                "options": [
                    {"label": "Fast boot", "description": ""},
                    {"label": "Dark mode", "description": "Easy on the eyes"},
                    {"label": "Telemetry", "description": ""},
                ],
            },
        ]
    }


def test_single_question_becomes_a_question_interaction() -> None:
    content = interaction_content("AskUserQuestion", question_input())

    assert content.kind == "question"
    assert content.title == "Which approach should we take?"
    # The body is the full readout: question, then each option with its
    # description - what the dashboard shows and what voice will speak.
    assert content.body.splitlines()[0] == "Which approach should we take?"
    assert "1. Option 1 (Recommended) — The safe path" in content.body
    assert "2. Option 2 — The fast path" in content.body
    assert content.options == ["Option 1 (Recommended)", "Option 2"]
    (group,) = content.questions
    assert group.id == "Which approach should we take?"
    assert group.prompt == "Which approach should we take?"
    assert [o.label for o in group.options] == ["Option 1 (Recommended)", "Option 2"]
    assert group.multi_select is False


def test_other_tools_keep_the_permission_presentation() -> None:
    input_data = {"command": "rm -rf build"}

    content = interaction_content("Bash", input_data)

    assert (content.kind, content.options, content.questions) == ("permission", [], [])
    assert (content.title, content.body) == permission_prompt("Bash", input_data)


def test_multi_question_and_multiselect_are_questions_now() -> None:
    # The ADR-0019 v1 limitation is lifted (ADR-0022): both shapes carry
    # structured groups instead of falling back to a raw-JSON permission.
    content = interaction_content("AskUserQuestion", two_question_input())

    assert content.kind == "question"
    assert content.title == "Which approach should we take? (+1 more)"
    assert len(content.questions) == 2
    assert content.questions[1].multi_select is True
    # Both questions read out in the body, multi-select flagged.
    assert "Which features do you want? (choose all that apply)" in content.body
    assert "2. Dark mode — Easy on the eyes" in content.body
    # Flat options exist only for the single-question single-select shape.
    assert content.options == []

    single_multi = interaction_content("AskUserQuestion", question_input(multiSelect=True))
    assert single_multi.kind == "question"
    assert single_multi.options == []
    assert single_multi.questions[0].multi_select is True


def test_duplicate_question_texts_get_suffixed_ids() -> None:
    duplicated = {
        "questions": [
            question_input()["questions"][0],  # type: ignore[index]
            question_input()["questions"][0],  # type: ignore[index]
        ]
    }

    content = interaction_content("AskUserQuestion", duplicated)

    assert [g.id for g in content.questions] == [
        "Which approach should we take?",
        "Which approach should we take? #2",
    ]
    # Prompts stay un-suffixed - the suffix is an internal key only.
    assert {g.prompt for g in content.questions} == {"Which approach should we take?"}


def test_malformed_question_input_stays_a_permission() -> None:
    for bad in (
        {},
        {"questions": "not a list"},
        {"questions": []},
        {"questions": [{"question": "ok?", "options": []}]},
        {"questions": [{"question": "", "options": [{"label": "A"}]}]},
        {"questions": [question_input()["questions"][0], "not a dict"]},  # type: ignore[index]
    ):
        assert interaction_content("AskUserQuestion", bad).kind == "permission", bad


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


def test_selections_map_every_question_to_answers() -> None:
    updated = questions_updated_input(
        two_question_input(),
        {
            "Which approach should we take?": ["Option 2"],
            "Which features do you want?": ["Fast boot", "Dark mode"],
        },
    )

    assert updated is not None
    assert updated["answers"] == {
        "Which approach should we take?": "Option 2",
        # The multi-select join format (", ") is asserted against real
        # transcripts - the answers contract is Claude-Code-owned.
        "Which features do you want?": "Fast boot, Dark mode",
    }
    assert updated["questions"] == two_question_input()["questions"]


def test_selections_match_case_insensitively_and_by_ordinal() -> None:
    updated = questions_updated_input(
        two_question_input(),
        {
            "Which approach should we take?": ["option 2"],
            "Which features do you want?": ["1", "dark mode"],
        },
    )

    assert updated is not None
    assert updated["answers"] == {
        "Which approach should we take?": "Option 2",
        "Which features do you want?": "Fast boot, Dark mode",
    }


def test_partial_or_bad_selections_map_to_nothing() -> None:
    base = two_question_input()
    good = {
        "Which approach should we take?": ["Option 2"],
        "Which features do you want?": ["Fast boot"],
    }
    # Missing one question entirely.
    assert questions_updated_input(base, {"Which approach should we take?": ["Option 2"]}) is None
    # One label matching nothing poisons the whole answer.
    bad_label = {**good, "Which features do you want?": ["Fast boot", "Blast processing"]}
    assert questions_updated_input(base, bad_label) is None
    # Two selections on a single-select question is not a choice either.
    two_on_single = {**good, "Which approach should we take?": ["Option 1 (Recommended)", "2"]}
    assert questions_updated_input(base, two_on_single) is None
    # Empty selections for a question.
    empty = {**good, "Which approach should we take?": []}
    assert questions_updated_input(base, empty) is None
    # Malformed input maps to nothing.
    assert questions_updated_input({}, good) is None


def test_selections_for_duplicate_question_texts_key_by_suffixed_id() -> None:
    duplicated = {
        "questions": [
            question_input()["questions"][0],  # type: ignore[index]
            question_input()["questions"][0],  # type: ignore[index]
        ]
    }

    updated = questions_updated_input(
        duplicated,
        {
            "Which approach should we take?": ["Option 1 (Recommended)"],
            "Which approach should we take? #2": ["Option 2"],
        },
    )

    # Claude Code's answers map keys by question text, so duplicates collapse
    # (last wins) - inherent to the agent's own contract, not ours.
    assert updated is not None
    assert updated["answers"] == {"Which approach should we take?": "Option 2"}
