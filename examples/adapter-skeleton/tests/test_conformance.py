"""The conformance suite is the adapter's test floor: inherit it, add your own.

Every Prodeo adapter must pass ``AdapterConformanceSuite`` (it is what keeps
capability declarations honest). Below it: one behavior test of our own.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from prodeo.adapters.interface import AgentAdapter
from prodeo.adapters.observations import OutputObservation
from prodeo.adapters.testing import AdapterConformanceSuite, recording_context
from prodeo_adapter_skeleton import SkeletonAdapter, manifest


class TestSkeletonConformance(AdapterConformanceSuite):
    @pytest.fixture
    def logs_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "logs"
        d.mkdir()
        (d / "session-1.log").write_text("hello from the agent\n")
        return d

    @pytest.fixture
    def adapter(self, logs_dir: Path) -> AgentAdapter:
        return SkeletonAdapter()

    @pytest.fixture
    def adapter_config(self, logs_dir: Path) -> dict[str, Any]:
        return {"logs_dir": str(logs_dir), "poll_interval_s": 0.05}


@pytest.mark.asyncio
async def test_watch_tails_appended_lines(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "s1.log"
    log.write_text("first\n")

    adapter = SkeletonAdapter()
    ctx, observations = recording_context(
        "skeleton", tmp_path / "data", {"logs_dir": str(logs), "poll_interval_s": 0.02}
    )
    await adapter.start(ctx)
    ref_list = await adapter.discover_sessions()
    assert [d.native_id for d in ref_list] == ["s1"]

    from prodeo.adapters.interface import SessionRef

    task = asyncio.create_task(
        adapter.watch(SessionRef(adapter="skeleton", native_id="s1", session_id="x"))
    )
    try:
        await asyncio.sleep(0.1)
        with log.open("a") as fh:
            fh.write("second\n")
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            texts = [o.text for o in observations if isinstance(o, OutputObservation)]
            if texts == ["first", "second"]:
                break
            await asyncio.sleep(0.02)
        assert texts == ["first", "second"]
    finally:
        task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await task
    await adapter.stop()


def test_manifest_is_an_adapter_plugin() -> None:
    m = manifest()
    assert m.kind == "adapter"
    assert m.factory().metadata.name == "skeleton"
