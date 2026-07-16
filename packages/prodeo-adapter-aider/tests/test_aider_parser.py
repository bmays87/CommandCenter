"""HistoryParser against a fixture corpus of real-shaped Aider logs."""

from pathlib import Path

from prodeo.adapters.observations import (
    Observation,
    OutputObservation,
    ToolObservation,
    ToolPhase,
    TurnObservation,
    TurnPhase,
)
from prodeo_adapter_aider.parser import HistoryParser

FIXTURES = Path(__file__).parent / "fixtures"


def _parse(text: str) -> tuple[HistoryParser, list[Observation]]:
    parser = HistoryParser(native_id="proj")
    out: list[Observation] = []
    for line in text.splitlines():
        out.extend(parser.feed_line(line))
    out.extend(parser.flush())
    return parser, out


def test_basic_fixture_full_walkthrough() -> None:
    parser, out = _parse((FIXTURES / "basic.history.md").read_text())

    users = [o for o in out if isinstance(o, OutputObservation) and o.role == "user"]
    assert [u.text for u in users] == [
        "add a retry to the fetch helper\nand cover it with a test",
        "looks good, thanks",
        "rename the helper to fetch_with_retry",
    ]

    assistants = [o for o in out if isinstance(o, OutputObservation) and o.role == "assistant"]
    assert len(assistants) == 3
    assert "exponential backoff" in assistants[0].text
    assert "```python" in assistants[0].text  # markdown passes through intact

    edits = [o for o in out if isinstance(o, ToolObservation)]
    assert [e.phase for e in edits] == [ToolPhase.FINISHED] * 3
    assert [e.detail for e in edits] == [
        "src/net/fetch.py",
        "tests/test_fetch.py",
        "src/net/fetch.py",
    ]
    assert all(e.tool == "edit" for e in edits)

    turns = [o for o in out if isinstance(o, TurnObservation)]
    assert [t.phase for t in turns] == [
        TurnPhase.STARTED,  # first prompt
        TurnPhase.COMPLETED,  # first tokens line
        TurnPhase.STARTED,
        TurnPhase.COMPLETED,
        TurnPhase.STARTED,
        TurnPhase.COMPLETED,
    ]

    meta = parser.meta
    assert meta.title == "add a retry to the fetch helper and cover it with a test"
    assert meta.model == "claude-sonnet-5"  # the latest run's model wins
    assert meta.agent_version == "0.85.1"
    assert meta.last_started_at is not None
    assert meta.last_started_at.strftime("%H:%M:%S") == "14:03:10"


def test_info_lines_become_system_output() -> None:
    _, out = _parse("> Git repo: .git with 42 files\n")
    (obs,) = out
    assert isinstance(obs, OutputObservation)
    assert obs.role == "system"
    assert "Git repo" in obs.text


def test_assistant_text_flushes_on_idle_not_per_line() -> None:
    parser = HistoryParser(native_id="proj")
    assert parser.feed_line("first paragraph line") == []
    assert parser.feed_line("second line") == []
    (obs,) = parser.flush()
    assert isinstance(obs, OutputObservation)
    assert obs.text == "first paragraph line\nsecond line"
    assert parser.flush() == []  # nothing buffered twice


def test_meta_dirty_reported_once() -> None:
    parser, _ = _parse("> Aider v0.85.1\n> Model: gpt-4o with diff edit format\n")
    assert parser.consume_meta_dirty() is True
    assert parser.consume_meta_dirty() is False


def test_garbage_never_raises() -> None:
    parser = HistoryParser(native_id="proj")
    for line in ["", "####", ">", "\x00binary\xff", "#### ", "# aider chat started at nonsense"]:
        parser.feed_line(line)  # must not raise
    parser.flush()
