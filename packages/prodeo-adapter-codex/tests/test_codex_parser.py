"""RolloutParser against a fixture corpus of real-shaped Codex rollouts."""

from pathlib import Path

from prodeo.adapters.observations import (
    Observation,
    OutputObservation,
    ToolObservation,
    ToolPhase,
    TurnObservation,
    TurnPhase,
)
from prodeo_adapter_codex.parser import RolloutParser

FIXTURES = Path(__file__).parent / "fixtures"
ROLLOUT = FIXTURES / "rollout-2026-07-15T09-12-44-0198b1a2.jsonl"


def _parse(text: str) -> tuple[RolloutParser, list[Observation]]:
    parser = RolloutParser(native_id="r1")
    out: list[Observation] = []
    for line in text.splitlines():
        out.extend(parser.feed_line(line))
    return parser, out


def test_fixture_full_walkthrough() -> None:
    parser, out = _parse(ROLLOUT.read_text())

    users = [o for o in out if isinstance(o, OutputObservation) and o.role == "user"]
    # The synthetic <environment_context> user message is filtered out.
    assert [u.text for u in users] == ["fix the flaky websocket test"]

    assistants = [o for o in out if isinstance(o, OutputObservation) and o.role == "assistant"]
    # agent_message event_msg is skipped; the response_item is the source of truth.
    assert [a.text for a in assistants] == [
        "Fixed the race by awaiting the handshake before asserting."
    ]

    tools = [o for o in out if isinstance(o, ToolObservation)]
    assert [(t.tool, t.phase) for t in tools] == [
        ("shell", ToolPhase.STARTED),
        ("shell", ToolPhase.FAILED),  # exit_code 1
        ("apply_patch", ToolPhase.STARTED),
        ("apply_patch", ToolPhase.FINISHED),
    ]
    assert tools[1].detail == "1 failed, 3 passed"

    turns = [o for o in out if isinstance(o, TurnObservation)]
    assert [t.phase for t in turns] == [TurnPhase.STARTED, TurnPhase.COMPLETED]

    opaque = [
        o for o in out if isinstance(o, OutputObservation) and o.metadata.get("opaque") is True
    ]
    assert len(opaque) == 1  # the unknown future record surfaced, not dropped
    assert opaque[0].metadata["record_type"] == "some_future_record"

    meta = parser.meta
    assert meta.session_id == "0198b1a2-aaaa-bbbb-cccc-d1e2f3a4b5c6"
    assert meta.project == "/home/me/src/app"
    assert meta.model == "gpt-5-codex"
    assert meta.git_branch == "main"
    assert meta.agent_version == "0.13.0"
    assert meta.title == "fix the flaky websocket test"


def test_timestamps_flow_into_observations() -> None:
    _, out = _parse(ROLLOUT.read_text())
    stamped = [o for o in out if isinstance(o, OutputObservation) and o.at is not None]
    assert stamped, "output observations carry rollout timestamps"
    assert stamped[0].at is not None and stamped[0].at.tzinfo is not None


def test_error_event_surfaces_as_system_output() -> None:
    _, out = _parse(
        '{"timestamp":"2026-07-15T10:00:00Z","type":"event_msg",'
        '"payload":{"type":"error","message":"stream disconnected"}}'
    )
    (obs,) = out
    assert isinstance(obs, OutputObservation)
    assert obs.role == "system"
    assert obs.metadata.get("error") is True
    assert "stream disconnected" in obs.text


def test_nested_session_meta_shape_is_accepted() -> None:
    parser, _ = _parse(
        '{"timestamp":"2026-07-15T10:00:00Z","type":"session_meta",'
        '"payload":{"meta":{"id":"sid-2","cwd":"/w","cli_version":"0.99.0"}}}'
    )
    assert parser.meta.session_id == "sid-2"
    assert parser.meta.project == "/w"


def test_meta_dirty_reported_once() -> None:
    parser, _ = _parse(ROLLOUT.read_text())
    assert parser.consume_meta_dirty() is True
    assert parser.consume_meta_dirty() is False


def test_garbage_never_raises() -> None:
    parser = RolloutParser(native_id="r1")
    for line in ["", "not json", "[1,2,3]", '{"type":null}', '{"payload":"str"}', "\x00\xff"]:
        parser.feed_line(line)  # must not raise
