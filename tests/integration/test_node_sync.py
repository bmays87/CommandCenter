"""Two machines, one hub: mirroring and command routing end to end (ADR-0026).

A real CCAN NodeHost (fake adapter, real bus/store/registry/mediation) runs
"worker-01"; a real hub stack mirrors it through NodeSync and routes
commands back through the API. The transport itself (mutual TLS, pinning)
is proven separately in test_ccan_pairing.py — here the channel is the
CCAN's real ASGI app, so what's under test is everything above TLS:
discovery on the node appearing in the hub's fleet, hub-side launch and
terminate landing on the node's adapter, and a node-opened interaction
being answered from the hub inbox.
"""

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from prodeo.adapters import AdapterManager
from prodeo.adapters.context import AdapterContext
from prodeo.adapters.interface import (
    AdapterCapabilities,
    AdapterMetadata,
    LaunchSpec,
    SessionRef,
)
from prodeo.adapters.observations import StateObservation
from prodeo.api import create_app
from prodeo.bus import InProcessEventBus
from prodeo.errors import RemoteNodeError
from prodeo.identity import ensure_identity
from prodeo.machines import MachineRegistry
from prodeo.machines.sync import NodeSync
from prodeo.mediation import (
    Answer,
    Interaction,
    InteractionKind,
    InteractionRequest,
    InteractionStatus,
    MediationService,
)
from prodeo.persistence import EventRecorder, SqliteEventStore
from prodeo.persistence.interface import EventQuery
from prodeo.plugins import PluginHost
from prodeo.presence import PresenceTracker
from prodeo.scheduler import SchedulerService
from prodeo.sessions import SessionDescriptor, SessionRegistry, SessionState
from prodeo_ccan.app import create_app as create_ccan_app
from prodeo_ccan.config import CcanConfig
from prodeo_ccan.node import NodeHost

pytestmark = pytest.mark.integration

TOKEN = "secret-token"


class FakeAdapter:
    """A control-capable adapter that records what the node asks of it."""

    def __init__(self) -> None:
        self.metadata = AdapterMetadata(name="fake", version="0")
        self.capabilities = AdapterCapabilities(observe=True, launch=True, terminate=True)
        self.discoverable: list[SessionDescriptor] = []
        self.launched: list[LaunchSpec] = []
        self.terminated: list[str] = []
        self._ctx: AdapterContext | None = None

    async def start(self, ctx: AdapterContext) -> None:
        self._ctx = ctx

    async def stop(self) -> None: ...

    async def discover_sessions(self) -> list[SessionDescriptor]:
        return list(self.discoverable)

    async def watch(self, session: SessionRef) -> None: ...

    async def launch(self, spec: LaunchSpec) -> SessionRef:
        self.launched.append(spec)
        native = f"launched-{len(self.launched)}"
        return SessionRef(adapter="fake", native_id=native, session_id="")

    async def terminate(self, session: SessionRef) -> None:
        self.terminated.append(session.native_id)
        assert self._ctx is not None
        await self._ctx.report(
            StateObservation(
                native_id=session.native_id,
                state=SessionState.STOPPED,
                reason="terminated",
                at=datetime.now(UTC),
            )
        )


class AsgiChannel:
    """NodeChannel over the CCAN's ASGI app — the transport minus TLS."""

    def __init__(self, app: Any) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://ccan"
        )

    async def forward(
        self, node: str, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        resp = await self._client.request(method, path, json=payload)
        if resp.status_code >= 400:
            raise RemoteNodeError(
                str(resp.json().get("detail", "error")), status_code=resp.status_code
            )
        return resp.json() if resp.content else None

    async def aclose(self) -> None:
        await self._client.aclose()


class Env:
    """One CCAN ("worker-01") and one hub ("hub"), wired minus TLS."""

    def __init__(self, tmp_path: Path) -> None:
        # --- the node
        self.ccan_config = CcanConfig.model_validate(
            {
                "hub": {"node": "hub", "certificate_pem": "PEM"},
                "enroll_token": "tok",
                "node_name": "worker-01",
                "data_dir": str(tmp_path / "ccan"),
                "discovery_interval_s": 0.05,
            }
        )
        self.adapter = FakeAdapter()
        self.host = NodeHost(
            self.ccan_config,
            plugins=PluginHost(InProcessEventBus(), entry_points_fn=lambda: []),
        )
        self.host.adapters.add(self.adapter)  # type: ignore[arg-type]  # satisfies the Protocol
        ccan_identity = ensure_identity(
            self.ccan_config.data_dir / "identity", common_name="worker-01"
        )
        self.ccan_app = create_ccan_app(self.ccan_config, ccan_identity, self.host)
        self.channel = AsgiChannel(self.ccan_app)

        # --- the hub
        self.bus = InProcessEventBus()
        self.store = SqliteEventStore(tmp_path / "hub" / "events.db")
        self.recorder = EventRecorder(self.bus, self.store)
        self.registry = SessionRegistry(self.bus, node="hub")
        self.mediation = MediationService(self.bus, node="hub")
        self.machines = MachineRegistry(self.bus, node="hub")
        self.manager = AdapterManager(
            self.bus,
            self.registry,
            self.mediation,
            data_dir=tmp_path / "hub",
            node="hub",
            discovery_interval=0,
        )
        self.sync = NodeSync(
            self.bus,
            self.store,
            self.registry,
            self.mediation,
            self.machines,
            self.channel,
            node="hub",
            poll_wait_s=0.0,
            reconcile_interval_s=0.05,
        )
        self.app = create_app(
            registry=self.registry,
            store=self.store,
            bus=self.bus,
            mediation=self.mediation,
            manager=self.manager,
            scheduler=SchedulerService(self.bus, self.manager, node="hub"),
            presence=PresenceTracker(),
            node="hub",
            version="0.0-test",
            api_token=TOKEN,
            machines=self.machines,
            gateway=self.channel,
        )

    async def start(self) -> None:
        await self.host.start()  # the app's lifespan doesn't run under ASGITransport
        await self.store.open()
        await self.recorder.start()
        await self.machines.add(node="worker-01", address="worker-01.lan")
        await self.sync.start()

    async def stop(self) -> None:
        await self.sync.stop()
        await self.recorder.stop()
        await self.bus.close()
        await self.store.close()
        await self.host.stop()
        await self.channel.aclose()


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> Any:
    environment = Env(tmp_path)
    await environment.start()
    yield environment
    await environment.stop()


def _client(env: Env) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url="http://hub",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


async def _until(check: Callable[[], Coroutine[Any, Any, bool]], what: str) -> None:
    async with asyncio.timeout(10):
        while not await check():
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_remote_sessions_mirror_and_commands_route(env: Env) -> None:
    # A session appears on the node; the hub's fleet sees it under its node.
    env.adapter.discoverable.append(SessionDescriptor(native_id="abc", title="remote work"))

    async def mirrored() -> bool:
        return env.registry.resolve("fake", "abc") is not None

    await _until(mirrored, "session mirrored")
    session = env.registry.resolve("fake", "abc")
    assert session is not None
    assert session.node == "worker-01"

    async with _client(env) as client:
        # Hub-side launch on the machine's tab lands on the node's adapter.
        machine = env.machines.get_by_node("worker-01")
        assert machine is not None
        resp = await client.post(
            "/api/sessions",
            json={"adapter": "fake", "prompt": "do it", "machine_id": machine.id},
        )
        assert resp.status_code == 201, resp.text
        launched = resp.json()
        assert launched["node"] == "worker-01"
        assert [s.prompt for s in env.adapter.launched] == ["do it"]

        # The launched session mirrors into the hub's fleet...
        async def launched_mirrored() -> bool:
            return env.registry.get(launched["id"]) is not None

        await _until(launched_mirrored, "launched session mirrored")

        # ...and terminate routes to the node; the resulting fact mirrors back.
        resp = await client.post(f"/api/sessions/{launched['id']}/terminate")
        assert resp.status_code == 200, resp.text
        assert env.adapter.terminated == ["launched-1"]

        async def stopped() -> bool:
            mirrored_session = env.registry.get(launched["id"])
            return mirrored_session is not None and mirrored_session.state is SessionState.STOPPED

        await _until(stopped, "terminate mirrored")


@pytest.mark.asyncio
async def test_remote_interaction_answers_from_the_hub_inbox(env: Env) -> None:
    session = await env.host.registry.upsert_discovered(
        "fake", SessionDescriptor(native_id="blocked")
    )
    delivered: list[Answer] = []

    async def deliver(_interaction: Interaction, answer: Answer) -> None:
        delivered.append(answer)

    interaction = await env.host.mediation.open(
        InteractionRequest(
            session_id=session.id,
            adapter="fake",
            native_id="i-1",
            kind=InteractionKind.PERMISSION,
            title="Allow the thing?",
        ),
        deliver,
    )

    async def mirrored() -> bool:
        return env.mediation.get(interaction.id) is not None

    await _until(mirrored, "interaction mirrored")
    hub_view = env.mediation.get(interaction.id)
    assert hub_view is not None
    assert hub_view.node == "worker-01"
    assert hub_view.status is InteractionStatus.PENDING

    # A hub reboot must not orphan-cancel the remote pending (ADR-0026).
    # Folding is synchronous but persistence is a bus subscriber: wait for
    # the mirrored fact to be durable before simulating the reboot.
    async def persisted() -> bool:
        rows = await env.store.query(EventQuery(type_pattern="interaction.*", limit=1))
        return bool(rows)

    await _until(persisted, "interaction persisted")
    reloaded = MediationService(env.bus, node="hub")
    await reloaded.rebuild(env.store)
    reloaded_view = reloaded.get(interaction.id)
    assert reloaded_view is not None
    assert reloaded_view.status is InteractionStatus.PENDING

    async with _client(env) as client:
        resp = await client.post(
            f"/api/interactions/{interaction.id}/answer", json={"decision": "allow"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "answered"

    # The node's waiting callback got the answer...
    assert [a.decision for a in delivered] == ["allow"]

    async def resolved() -> bool:
        current = env.mediation.get(interaction.id)
        return current is not None and current.status is InteractionStatus.ANSWERED

    # ...and the resolution mirrored back into the hub's inbox state.
    await _until(resolved, "answer mirrored")


@pytest.mark.asyncio
async def test_ccan_config_accepts_adapter_settings(tmp_path: Path) -> None:
    doc = {
        "hub": {"node": "hub", "certificate_pem": "PEM"},
        "enroll_token": "tok",
        "adapters": {"claude-code": {"idle_timeout_s": 60}},
    }
    path = tmp_path / "ccan.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    config = CcanConfig.load(path)
    assert config.adapters["claude-code"]["idle_timeout_s"] == 60
