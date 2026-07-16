"""Phase 3 exit criterion: a scheduled agent run happens unattended and is
summarized — through the composed server and the real API surface."""

import asyncio
from pathlib import Path

import httpx
import pytest

from prodeo.adapters import (
    AdapterCapabilities,
    AdapterContext,
    AdapterMetadata,
    LaunchSpec,
    ObserveOnlyAdapter,
    SessionRef,
)
from prodeo.config import Settings
from prodeo.events import types as ev
from prodeo.persistence import EventQuery
from prodeo.server import Server
from prodeo.sessions import SessionDescriptor

pytestmark = pytest.mark.integration

TOKEN = "integration-token"


class FakeAgentAdapter(ObserveOnlyAdapter):
    """Launch-capable stand-in for a real agent."""

    def __init__(self) -> None:
        self.metadata = AdapterMetadata(name="fake-agent", version="0.0.1")
        self.capabilities = AdapterCapabilities(launch=True, terminate=True)
        self.launched: list[LaunchSpec] = []

    async def start(self, ctx: AdapterContext) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def discover_sessions(self) -> list[SessionDescriptor]:
        return []

    async def watch(self, session: SessionRef) -> None:
        pass

    async def launch(self, spec: LaunchSpec) -> SessionRef:
        self.launched.append(spec)
        return SessionRef(
            adapter="fake-agent", native_id=f"run-{len(self.launched)}", session_id=""
        )

    async def terminate(self, session: SessionRef) -> None:
        pass


@pytest.mark.asyncio
async def test_scheduled_run_fires_and_is_summarized(tmp_path: Path) -> None:
    settings = Settings(
        node_name="orchestrate-test",
        data_dir=tmp_path,
        api_port=0,
        api_token=TOKEN,
        adapters={
            "claude-code": {"projects_dir": str(tmp_path / "none")},
            "codex": {"sessions_dir": str(tmp_path / "none")},
        },
        discovery_interval_s=0,
        dashboard_dir=tmp_path / "no-dashboard",
        scheduler_timezone="UTC",
    )
    server = Server(settings)
    adapter = FakeAgentAdapter()
    server.adapters.add(adapter)

    await server.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{server.api.port}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            # Define the schedule over the API, as a user would.
            created = await client.post(
                "/api/schedules",
                json={
                    "name": "nightly-refactor",
                    "cron": "0 2 * * *",
                    "adapter": "fake-agent",
                    "prompt": "tidy the imports",
                },
            )
            assert created.status_code == 201
            schedule = created.json()
            assert schedule["next_run_at"] is not None

            # "Unattended" without waiting for 02:00: the manual trigger uses
            # the exact same firing path as the cron loop.
            fired = await client.post(f"/api/schedules/{schedule['id']}/trigger")
            assert fired.status_code == 200

            assert [s.prompt for s in adapter.launched] == ["tidy the imports"]

            # The launched session is registered and visible over the API.
            sessions = (await client.get("/api/sessions")).json()["sessions"]
            launched = [s for s in sessions if s["adapter"] == "fake-agent"]
            assert len(launched) == 1

        # The triggered fact reaches the persisted log (recorder is async).
        async def triggered_events() -> list:  # type: ignore[type-arg]
            return await server.store.query(EventQuery(type_pattern=ev.SCHEDULE_TRIGGERED))

        deadline = asyncio.get_running_loop().time() + 5
        while not await triggered_events() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        (triggered,) = await triggered_events()
        assert triggered.payload["name"] == "nightly-refactor"
        assert triggered.session_id is not None

        # ... and the daily summary reports the scheduled run.
        summary = await server.summary.run_once()
        assert summary.type == ev.SUMMARY_GENERATED
        assert summary.payload["stats"]["schedule_triggers"] == 1
        assert "nightly-refactor" in summary.payload["digest"]
    finally:
        await server.stop()
