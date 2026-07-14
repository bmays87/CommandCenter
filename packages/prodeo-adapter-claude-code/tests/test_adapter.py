"""Adapter behavior: discovery, live tailing, offset persistence, conformance."""

import asyncio
import contextlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from prodeo.adapters import SessionRef
from prodeo.adapters.observations import (
    Observation,
    OutputObservation,
    StateObservation,
)
from prodeo.adapters.testing import AdapterConformanceSuite, recording_context
from prodeo.sessions.state import SessionState
from prodeo_adapter_claude_code import ClaudeCodeAdapter, create_adapter

FIXTURES = Path(__file__).parent / "fixtures"
NATIVE_ID = "11111111-2222-3333-4444-555555555555"


def make_projects_dir(tmp_path: Path, *, stale: bool = False) -> Path:
    projects = tmp_path / "projects"
    project = projects / "f--home-dev-repo"
    project.mkdir(parents=True)
    transcript = project / f"{NATIVE_ID}.jsonl"
    shutil.copy(FIXTURES / "session-basic.jsonl", transcript)
    if stale:
        old = time.time() - 7200
        os.utime(transcript, (old, old))
    return projects


def config_for(projects: Path, **overrides: object) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "projects_dir": str(projects),
        "poll_interval_s": 0.02,
        "idle_timeout_s": 1800,
    }
    cfg.update(overrides)
    return cfg


async def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_discovery_describes_active_session(tmp_path: Path) -> None:
    projects = make_projects_dir(tmp_path)
    adapter = create_adapter()
    ctx, _ = recording_context("claude-code", tmp_path / "data", config_for(projects))
    await adapter.start(ctx)

    (desc,) = await adapter.discover_sessions()

    assert desc.native_id == NATIVE_ID
    assert desc.title == "Fix failing auth test"
    assert desc.project == "/home/dev/repo"
    assert desc.model == "claude-fable-5"
    assert desc.state == SessionState.RUNNING  # fresh mtime
    assert desc.metadata["git_branch"] == "main"
    await adapter.stop()


@pytest.mark.asyncio
async def test_discovery_marks_idle_sessions_completed(tmp_path: Path) -> None:
    projects = make_projects_dir(tmp_path, stale=True)
    adapter = create_adapter()
    ctx, _ = recording_context("claude-code", tmp_path / "data", config_for(projects))
    await adapter.start(ctx)

    (desc,) = await adapter.discover_sessions()

    assert desc.state == SessionState.COMPLETED
    await adapter.stop()


async def run_watch(
    adapter: ClaudeCodeAdapter, observations: list[Observation], min_count: int
) -> asyncio.Task[None]:
    await adapter.discover_sessions()
    ref = SessionRef(adapter="claude-code", native_id=NATIVE_ID, session_id="s1")
    task = asyncio.create_task(adapter.watch(ref))
    await wait_for(lambda: len(observations) >= min_count)
    return task


@pytest.mark.asyncio
async def test_watch_replays_history_and_tails_new_lines(tmp_path: Path) -> None:
    projects = make_projects_dir(tmp_path)
    transcript = projects / "f--home-dev-repo" / f"{NATIVE_ID}.jsonl"
    adapter = create_adapter()
    ctx, observations = recording_context("claude-code", tmp_path / "data", config_for(projects))
    await adapter.start(ctx)
    task = await run_watch(adapter, observations, min_count=8)

    outputs = [o for o in observations if isinstance(o, OutputObservation)]
    assert any(o.role == "user" for o in outputs)
    assert any(o.role == "assistant" for o in outputs)

    seen = len(observations)
    line = json.dumps(
        {
            "type": "assistant",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "model": "claude-fable-5",
                "content": [{"type": "text", "text": "LIVE TAIL"}],
            },
            "timestamp": "2026-07-12T10:05:00.000Z",
        }
    )
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    await wait_for(
        lambda: any(
            isinstance(o, StateObservation) and o.state == SessionState.RUNNING
            for o in observations[seen:]
        )
    )

    tail = observations[seen:]
    assert any(isinstance(o, OutputObservation) and o.text == "LIVE TAIL" for o in tail)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await adapter.stop()


@pytest.mark.asyncio
async def test_offsets_persist_so_restart_does_not_replay(tmp_path: Path) -> None:
    projects = make_projects_dir(tmp_path)
    data_dir = tmp_path / "data"

    first = create_adapter()
    ctx, observations = recording_context("claude-code", data_dir, config_for(projects))
    await first.start(ctx)
    task = await run_watch(first, observations, min_count=8)
    await wait_for(lambda: (data_dir / "offsets.json").exists())
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await first.stop()

    second = create_adapter()
    ctx2, observations2 = recording_context("claude-code", data_dir, config_for(projects))
    await second.start(ctx2)
    await second.discover_sessions()
    ref = SessionRef(adapter="claude-code", native_id=NATIVE_ID, session_id="s1")
    task2 = asyncio.create_task(second.watch(ref))
    await asyncio.sleep(0.3)
    task2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task2

    assert not [o for o in observations2 if isinstance(o, OutputObservation)]
    await second.stop()


@pytest.mark.asyncio
async def test_watch_completes_idle_session_and_returns(tmp_path: Path) -> None:
    projects = make_projects_dir(tmp_path)
    adapter = create_adapter()
    ctx, observations = recording_context(
        "claude-code", tmp_path / "data", config_for(projects, idle_timeout_s=0.2)
    )
    await adapter.start(ctx)
    task = await run_watch(adapter, observations, min_count=8)

    await asyncio.wait_for(task, timeout=5)  # watch must end on its own

    final = observations[-1]
    assert isinstance(final, StateObservation)
    assert final.state == SessionState.COMPLETED
    assert final.reason == "idle"
    await adapter.stop()


@pytest.mark.asyncio
async def test_watch_reports_stopped_when_transcript_vanishes(tmp_path: Path) -> None:
    projects = make_projects_dir(tmp_path)
    transcript = projects / "f--home-dev-repo" / f"{NATIVE_ID}.jsonl"
    adapter = create_adapter()
    ctx, observations = recording_context("claude-code", tmp_path / "data", config_for(projects))
    await adapter.start(ctx)
    task = await run_watch(adapter, observations, min_count=8)

    transcript.unlink()
    await asyncio.wait_for(task, timeout=5)

    final = observations[-1]
    assert isinstance(final, StateObservation)
    assert final.state == SessionState.STOPPED
    await adapter.stop()


class TestClaudeCodeConformance(AdapterConformanceSuite):
    """The reusable conformance suite, run against this adapter."""

    @pytest.fixture
    def adapter(self) -> ClaudeCodeAdapter:
        return create_adapter()

    @pytest.fixture
    def adapter_config(self, tmp_path: Path) -> dict[str, Any]:
        return config_for(make_projects_dir(tmp_path))
