"""Codex adapter: conformance suite + discovery/watch behavior."""

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from prodeo.adapters.interface import AgentAdapter, SessionRef
from prodeo.adapters.observations import OutputObservation, StateObservation
from prodeo.adapters.testing import AdapterConformanceSuite, recording_context
from prodeo.sessions.state import SessionState
from prodeo_adapter_codex import CodexAdapter, manifest

FIXTURE = Path(__file__).parent / "fixtures" / "rollout-2026-07-15T09-12-44-0198b1a2.jsonl"


def _sessions_tree(tmp_path: Path) -> Path:
    """A date-sharded sessions dir holding the fixture rollout."""
    day_dir = tmp_path / "sessions" / "2026" / "07" / "15"
    day_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, day_dir / FIXTURE.name)
    return tmp_path / "sessions"


class TestCodexConformance(AdapterConformanceSuite):
    @pytest.fixture
    def sessions_dir(self, tmp_path: Path) -> Path:
        return _sessions_tree(tmp_path)

    @pytest.fixture
    def adapter(self, sessions_dir: Path) -> AgentAdapter:
        return CodexAdapter()

    @pytest.fixture
    def adapter_config(self, sessions_dir: Path) -> dict[str, Any]:
        return {"sessions_dir": str(sessions_dir), "poll_interval_s": 0.05}


@pytest.mark.asyncio
async def test_discovery_reads_session_meta(tmp_path: Path) -> None:
    sessions_dir = _sessions_tree(tmp_path)
    adapter = CodexAdapter()
    ctx, _ = recording_context("codex", tmp_path / "data", {"sessions_dir": str(sessions_dir)})
    await adapter.start(ctx)
    descriptors = await adapter.discover_sessions()
    await adapter.stop()

    (desc,) = descriptors
    assert desc.native_id == FIXTURE.stem
    assert desc.project == "/home/me/src/app"
    assert desc.model == "gpt-5-codex"
    assert desc.title == "fix the flaky websocket test"
    assert desc.metadata["codex_session_id"].startswith("0198b1a2")
    assert desc.metadata["agent_version"] == "0.13.0"


@pytest.mark.asyncio
async def test_watch_replays_and_tails(tmp_path: Path) -> None:
    sessions_dir = _sessions_tree(tmp_path)
    rollout = sessions_dir / "2026" / "07" / "15" / FIXTURE.name
    adapter = CodexAdapter()
    ctx, observations = recording_context(
        "codex", tmp_path / "data", {"sessions_dir": str(sessions_dir), "poll_interval_s": 0.02}
    )
    await adapter.start(ctx)
    ref = SessionRef(adapter="codex", native_id=FIXTURE.stem, session_id="s")
    task = asyncio.create_task(adapter.watch(ref))
    try:

        def outputs() -> list[str]:
            return [o.text for o in observations if isinstance(o, OutputObservation)]

        await _until(lambda: any("flaky websocket" in t for t in outputs()))

        # Live tail: append a new user turn to the rollout.
        line = {
            "timestamp": "2026-07-15T09:20:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "also update the changelog"}],
            },
        }
        with rollout.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
        await _until(lambda: any("update the changelog" in t for t in outputs()))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await adapter.stop()


@pytest.mark.asyncio
async def test_watch_reports_completed_on_idle_and_offsets_survive_restart(
    tmp_path: Path,
) -> None:
    sessions_dir = _sessions_tree(tmp_path)
    data_dir = tmp_path / "data"

    async def run_watch_once() -> list[Any]:
        adapter = CodexAdapter()
        ctx, observations = recording_context(
            "codex",
            data_dir,
            {"sessions_dir": str(sessions_dir), "poll_interval_s": 0.02, "idle_timeout_s": 0.2},
        )
        await adapter.start(ctx)
        ref = SessionRef(adapter="codex", native_id=FIXTURE.stem, session_id="s")
        await asyncio.wait_for(adapter.watch(ref), timeout=5)
        await adapter.stop()
        return observations

    first = await run_watch_once()
    states = [o for o in first if isinstance(o, StateObservation)]
    assert states and states[-1].state is SessionState.COMPLETED

    second = await run_watch_once()
    replayed = [o for o in second if isinstance(o, OutputObservation)]
    assert replayed == []  # offsets persisted: no duplicate history events


def test_manifest_is_an_adapter_plugin() -> None:
    m = manifest()
    assert m.kind == "adapter"
    assert m.factory().metadata.name == "codex"


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")
