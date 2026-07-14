"""Parser behavior against the fixture corpus (catches upstream format drift)."""

from pathlib import Path

from prodeo.adapters.observations import (
    Observation,
    OutputObservation,
    ToolObservation,
    ToolPhase,
    TurnObservation,
    TurnPhase,
)
from prodeo_adapter_claude_code.parser import TranscriptParser

FIXTURES = Path(__file__).parent / "fixtures"


def parse_fixture(name: str) -> tuple[TranscriptParser, list[Observation]]:
    parser = TranscriptParser(native_id="fixture")
    observations: list[Observation] = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        observations.extend(parser.feed_line(line))
    return parser, observations


def test_basic_session_produces_expected_observation_sequence() -> None:
    _, obs = parse_fixture("session-basic.jsonl")
    kinds = [type(o).__name__ for o in obs]
    assert kinds == [
        "TurnObservation",  # human prompt begins a turn
        "OutputObservation",  # the prompt itself
        "OutputObservation",  # assistant text
        "ToolObservation",  # Bash started
        "ToolObservation",  # Bash finished
        "OutputObservation",  # assistant wrap-up
        "TurnObservation",  # end_turn
        "OutputObservation",  # unknown record type, surfaced opaquely
    ]


def test_user_prompt_and_turn() -> None:
    _, obs = parse_fixture("session-basic.jsonl")
    turn, prompt = obs[0], obs[1]
    assert isinstance(turn, TurnObservation) and turn.phase is TurnPhase.STARTED
    assert isinstance(prompt, OutputObservation)
    assert prompt.role == "user"
    assert "failing test" in prompt.text
    assert prompt.at is not None and prompt.at.year == 2026


def test_tool_use_and_result_are_paired_by_id() -> None:
    _, obs = parse_fixture("session-basic.jsonl")
    started, finished = obs[3], obs[4]
    assert isinstance(started, ToolObservation) and started.phase is ToolPhase.STARTED
    assert started.tool == "Bash"
    assert "pytest" in started.detail
    assert isinstance(finished, ToolObservation) and finished.phase is ToolPhase.FINISHED
    assert finished.tool == "Bash"  # name recovered from the pending tool map
    assert finished.tool_use_id == started.tool_use_id == "toolu_01"
    assert "1 failed" in finished.detail


def test_end_turn_emits_turn_completed() -> None:
    _, obs = parse_fixture("session-basic.jsonl")
    last_turn = obs[6]
    assert isinstance(last_turn, TurnObservation) and last_turn.phase is TurnPhase.COMPLETED


def test_unknown_record_type_is_opaque_not_fatal() -> None:
    _, obs = parse_fixture("session-basic.jsonl")
    opaque = obs[7]
    assert isinstance(opaque, OutputObservation)
    assert opaque.role == "system"
    assert opaque.metadata == {"record_type": "wibble-record", "opaque": True}


def test_thinking_sidechain_meta_and_noise_records_are_not_surfaced() -> None:
    _, obs = parse_fixture("session-basic.jsonl")
    texts = [o.text for o in obs if isinstance(o, OutputObservation)]
    assert not any("chain of thought" in t for t in texts)
    assert not any("sidechain" in t for t in texts)
    assert not any("housekeeping" in t for t in texts)


def test_metadata_extraction() -> None:
    parser, _ = parse_fixture("session-basic.jsonl")
    meta = parser.meta
    assert meta.title == "Fix failing auth test"  # ai-title wins over first prompt
    assert meta.project == "/home/dev/repo"
    assert meta.model == "claude-fable-5"
    assert meta.git_branch == "main"
    assert meta.agent_version == "2.1.0"
    assert meta.last_timestamp is not None


def test_garbage_lines_are_ignored() -> None:
    parser = TranscriptParser(native_id="x")
    assert parser.feed_line("") == []
    assert parser.feed_line("{not json") == []
    assert parser.feed_line('"just a string"') == []
