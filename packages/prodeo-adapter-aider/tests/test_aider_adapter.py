"""Aider adapter: conformance suite + discovery/watch behavior."""

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from prodeo.adapters.interface import AgentAdapter, SessionRef
from prodeo.adapters.observations import (
    OutputObservation,
    SessionObservation,
    StateObservation,
)
from prodeo.adapters.testing import AdapterConformanceSuite, recording_context
from prodeo.sessions.state import SessionState
from prodeo_adapter_aider import AiderAdapter, manifest

FIXTURE = Path(__file__).parent / "fixtures" / "basic.history.md"


def _project_with_history(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / ".aider.chat.history.md").write_text(FIXTURE.read_text())
    return project


class TestAiderConformance(AdapterConformanceSuite):
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        return _project_with_history(tmp_path)

    @pytest.fixture
    def adapter(self, project: Path) -> AgentAdapter:
        return AiderAdapter()

    @pytest.fixture
    def adapter_config(self, project: Path) -> dict[str, Any]:
        return {"projects": [str(project)], "poll_interval_s": 0.05}


@pytest.mark.asyncio
async def test_discovery_one_session_per_project(tmp_path: Path) -> None:
    project = _project_with_history(tmp_path)
    empty = tmp_path / "no-aider-here"
    empty.mkdir()

    adapter = AiderAdapter()
    ctx, _ = recording_context("aider", tmp_path / "data", {"projects": [str(project), str(empty)]})
    await adapter.start(ctx)
    descriptors = await adapter.discover_sessions()
    await adapter.stop()

    (desc,) = descriptors  # projects without a history file are not sessions
    assert desc.native_id == str(project)
    assert desc.project == str(project)
    assert desc.state is SessionState.RUNNING  # freshly written file
    assert desc.metadata["history"].endswith(".aider.chat.history.md")


@pytest.mark.asyncio
async def test_watch_replays_history_and_tails_new_lines(tmp_path: Path) -> None:
    project = _project_with_history(tmp_path)
    history = project / ".aider.chat.history.md"
    adapter = AiderAdapter()
    ctx, observations = recording_context(
        "aider", tmp_path / "data", {"projects": [str(project)], "poll_interval_s": 0.02}
    )
    await adapter.start(ctx)
    ref = SessionRef(adapter="aider", native_id=str(project), session_id="s")
    task = asyncio.create_task(adapter.watch(ref))
    try:

        def texts(role: str) -> list[str]:
            return [
                o.text for o in observations if isinstance(o, OutputObservation) and o.role == role
            ]

        await _until(lambda: len(texts("user")) >= 3)
        assert texts("user")[0].startswith("add a retry")

        # The session's descriptive fields surfaced from the parsed history.
        metas = [o for o in observations if isinstance(o, SessionObservation)]
        await _until(lambda: any(m.descriptor.model for m in metas) or bool(metas))
        assert any(m.descriptor.title.startswith("add a retry") for m in metas)

        # Live tail: a new prompt appended to the file arrives as output.
        with history.open("a") as fh:
            fh.write("\n#### one more thing\n\nOn it.\n")
        await _until(lambda: "one more thing" in texts("user"))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await adapter.stop()


@pytest.mark.asyncio
async def test_watch_reports_completed_on_idle(tmp_path: Path) -> None:
    project = _project_with_history(tmp_path)
    adapter = AiderAdapter()
    ctx, observations = recording_context(
        "aider",
        tmp_path / "data",
        {"projects": [str(project)], "poll_interval_s": 0.02, "idle_timeout_s": 0.2},
    )
    await adapter.start(ctx)
    ref = SessionRef(adapter="aider", native_id=str(project), session_id="s")

    await asyncio.wait_for(adapter.watch(ref), timeout=5)  # returns when idle
    await adapter.stop()

    states = [o for o in observations if isinstance(o, StateObservation)]
    assert states and states[-1].state is SessionState.COMPLETED
    assert states[-1].reason == "idle"


@pytest.mark.asyncio
async def test_restart_does_not_replay_history(tmp_path: Path) -> None:
    project = _project_with_history(tmp_path)
    data_dir = tmp_path / "data"

    async def run_watch_once() -> list[str]:
        adapter = AiderAdapter()
        ctx, observations = recording_context(
            "aider",
            data_dir,
            {"projects": [str(project)], "poll_interval_s": 0.02, "idle_timeout_s": 0.3},
        )
        await adapter.start(ctx)
        ref = SessionRef(adapter="aider", native_id=str(project), session_id="s")
        await asyncio.wait_for(adapter.watch(ref), timeout=5)
        await adapter.stop()
        return [o.text for o in observations if isinstance(o, OutputObservation)]

    first = await run_watch_once()
    second = await run_watch_once()  # same data dir: offsets persisted
    assert any("add a retry" in t for t in first)
    assert not any("add a retry" in t for t in second)


def test_manifest_is_an_adapter_plugin() -> None:
    m = manifest()
    assert m.kind == "adapter"
    assert m.factory().metadata.name == "aider"


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")
