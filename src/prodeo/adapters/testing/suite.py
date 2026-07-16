"""The adapter conformance tests (see package docstring for usage)."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from prodeo.adapters.context import AdapterContext
from prodeo.adapters.interface import (
    ADAPTER_API_VERSION,
    AgentAdapter,
    InteractionRef,
    LaunchSpec,
    SessionRef,
)
from prodeo.adapters.observations import Observation
from prodeo.errors import CapabilityNotSupportedError
from prodeo.mediation.model import Answer
from prodeo.sessions.model import SessionDescriptor


def recording_context(
    adapter_name: str, data_dir: Path, config: dict[str, Any] | None = None
) -> tuple[AdapterContext, list[Observation]]:
    """An AdapterContext whose reports accumulate into the returned list."""
    observations: list[Observation] = []

    async def report(obs: Observation) -> None:
        observations.append(obs)

    ctx = AdapterContext(
        adapter_name=adapter_name, report=report, config=config or {}, data_dir=data_dir
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    return ctx, observations


class AdapterConformanceSuite:
    """Inherit and provide an ``adapter`` fixture; the tests come for free."""

    #: How long test_watch waits for the first observation.
    watch_timeout: float = 5.0

    @pytest.fixture
    def adapter(self, tmp_path: Path) -> AgentAdapter:
        raise NotImplementedError("conformance subclasses must provide an `adapter` fixture")

    @pytest.fixture
    def adapter_config(self) -> dict[str, Any]:
        """Override to pass config to the adapter under test."""
        return {}

    async def provoke_activity(self, adapter: AgentAdapter) -> None:
        """Hook: make the watched session produce observations (default: no-op,
        which suits adapters that replay existing history on watch)."""

    @contextlib.asynccontextmanager
    async def running(
        self, adapter: AgentAdapter, tmp_path: Path, config: dict[str, Any] | None = None
    ) -> AsyncIterator[tuple[AdapterContext, list[Observation]]]:
        ctx, observations = recording_context(
            adapter.metadata.name, tmp_path / "adapter-data", config
        )
        await adapter.start(ctx)
        try:
            yield ctx, observations
        finally:
            await adapter.stop()

    # ------------------------------------------------------------ the suite

    def test_metadata_is_valid(self, adapter: AgentAdapter) -> None:
        assert adapter.metadata.name, "adapter must have a name"
        assert adapter.metadata.version, "adapter must have a version"
        assert adapter.metadata.adapter_api_version == ADAPTER_API_VERSION

    def test_observe_capability_is_always_true(self, adapter: AgentAdapter) -> None:
        assert adapter.capabilities.observe is True

    @pytest.mark.asyncio
    async def test_lifecycle_start_stop_is_clean_and_stop_is_idempotent(
        self, adapter: AgentAdapter, tmp_path: Path, adapter_config: dict[str, Any]
    ) -> None:
        async with self.running(adapter, tmp_path, adapter_config):
            pass
        await adapter.stop()  # second stop must not raise

    @pytest.mark.asyncio
    async def test_undeclared_capabilities_raise(
        self, adapter: AgentAdapter, tmp_path: Path, adapter_config: dict[str, Any]
    ) -> None:
        """Capability honesty: control methods not declared must refuse."""
        ref = SessionRef(adapter=adapter.metadata.name, native_id="x", session_id="x")
        async with self.running(adapter, tmp_path, adapter_config):
            caps = adapter.capabilities
            expected = (CapabilityNotSupportedError, NotImplementedError)
            if not caps.launch:
                with pytest.raises(expected):
                    await adapter.launch(LaunchSpec())
            if not caps.terminate:
                with pytest.raises(expected):
                    await adapter.terminate(ref)
            if not caps.send_prompts:
                with pytest.raises(expected):
                    await adapter.send_prompt(ref, "hello")
            if not (caps.respond_to_permissions or caps.answer_questions):
                iref = InteractionRef(
                    adapter=adapter.metadata.name,
                    session_native_id="x",
                    interaction_id="x",
                    native_id="x",
                )
                with pytest.raises(expected):
                    await adapter.respond(iref, Answer(decision="deny"))

    @pytest.mark.asyncio
    async def test_discovery_returns_valid_unique_descriptors(
        self, adapter: AgentAdapter, tmp_path: Path, adapter_config: dict[str, Any]
    ) -> None:
        async with self.running(adapter, tmp_path, adapter_config):
            descriptors = await adapter.discover_sessions()
            assert all(isinstance(d, SessionDescriptor) for d in descriptors)
            native_ids = [d.native_id for d in descriptors]
            assert len(native_ids) == len(set(native_ids)), "native_ids must be unique"

    @pytest.mark.asyncio
    async def test_watch_reports_valid_observations(
        self, adapter: AgentAdapter, tmp_path: Path, adapter_config: dict[str, Any]
    ) -> None:
        async with self.running(adapter, tmp_path, adapter_config) as (_ctx, observations):
            descriptors = await adapter.discover_sessions()
            if not descriptors:
                pytest.skip("adapter fixture provided no sessions to watch")
            ref = SessionRef(
                adapter=adapter.metadata.name,
                native_id=descriptors[0].native_id,
                session_id="conformance",
            )
            task = asyncio.create_task(adapter.watch(ref))
            try:
                await self.provoke_activity(adapter)
                deadline = asyncio.get_running_loop().time() + self.watch_timeout
                while not observations and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.05)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            assert observations, "watch produced no observations within the timeout"
