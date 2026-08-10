"""The /api/apps surface: listing, start/stop/restart, autostart, auth."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from prodeo.adapters import AdapterManager
from prodeo.api import create_app
from prodeo.apps import AppManifest, AppSupervisor
from prodeo.bus import InProcessEventBus
from prodeo.extensions import ExtensionService, JsonFileConfigStore
from prodeo.mediation import MediationService
from prodeo.persistence import SqliteEventStore
from prodeo.presence import PresenceTracker
from prodeo.scheduler import SchedulerService
from prodeo.sessions import SessionRegistry

TOKEN = "secret-token"


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        return 0

    def terminate(self) -> None:
        self._exited.set()

    def kill(self) -> None:
        self._exited.set()


async def _spawn(_manifest: AppManifest, _env: dict[str, str]) -> Any:
    return FakeProcess()


def _manifest() -> AppManifest:
    return AppManifest(
        name="mjolnir",
        version="0.1.0",
        command=["prodeo-mjolnir"],
        env_prefix="MJOLNIR_",
        presence_client_id="mjolnir",
        description="Voice client",
    )


def _app(
    tmp_path: Path, *, api_token: str | None = TOKEN, wire: bool = True
) -> tuple[FastAPI, AppSupervisor | None, ExtensionService]:
    bus = InProcessEventBus()
    registry = SessionRegistry(bus)
    mediation = MediationService(bus)
    manager = AdapterManager(bus, registry, mediation, data_dir=tmp_path, discovery_interval=0)
    store = JsonFileConfigStore(tmp_path / "extensions.json")
    extensions = ExtensionService(inventory_fn=lambda: [], env_config={}, store=store)

    async def config_fn(_name: str) -> dict[str, Any]:
        return {}

    async def autostart_fn() -> list[str]:
        return list((await store.settings()).autostart)

    supervisor = (
        AppSupervisor(
            config_fn=config_fn,
            autostart_fn=autostart_fn,
            manifests_fn=lambda: [_manifest()],
            spawn_fn=_spawn,
        )
        if wire
        else None
    )
    api = create_app(
        registry=registry,
        store=SqliteEventStore(tmp_path / "events.db"),
        bus=bus,
        mediation=mediation,
        manager=manager,
        scheduler=SchedulerService(bus, manager, node="test-node"),
        presence=PresenceTracker(),
        node="test-node",
        version="0.0-test",
        extensions=extensions,
        apps=supervisor,
        api_token=api_token,
    )
    return api, supervisor, extensions


def _client(api: FastAPI, *, token: str | None = TOKEN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://api", headers=headers
    )


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, AppSupervisor]]:
    api, supervisor, _ = _app(tmp_path)
    assert supervisor is not None
    await supervisor.start()
    async with _client(api) as client:
        yield client, supervisor
    await supervisor.stop()


@pytest.mark.asyncio
async def test_lists_installed_apps_stopped_by_default(
    env: tuple[httpx.AsyncClient, AppSupervisor],
) -> None:
    client, _ = env
    body = (await client.get("/api/apps")).json()
    (app,) = body["apps"]
    assert app["name"] == "mjolnir"
    assert app["state"] == "stopped"
    assert app["autostart"] is False  # opt-in, never uninvited


@pytest.mark.asyncio
async def test_start_stop_restart(env: tuple[httpx.AsyncClient, AppSupervisor]) -> None:
    client, _ = env

    started = (await client.post("/api/apps/mjolnir/start")).json()
    assert started["state"] in {"starting", "running"}

    listed = (await client.get("/api/apps")).json()["apps"][0]
    assert listed["pid"] == 4242

    restarted = (await client.post("/api/apps/mjolnir/restart")).json()
    assert restarted["state"] in {"starting", "running"}

    stopped = (await client.post("/api/apps/mjolnir/stop")).json()
    assert stopped["state"] == "stopped"


@pytest.mark.asyncio
async def test_autostart_persists_to_the_extension_settings(tmp_path: Path) -> None:
    api, supervisor, extensions = _app(tmp_path)
    assert supervisor is not None
    await supervisor.start()
    async with _client(api) as client:
        body = (await client.put("/api/apps/mjolnir/autostart", json={"autostart": True})).json()
        assert body["autostart"] is True
        # Persisted where the next boot will read it.
        assert (await extensions.settings()).autostart == ["mjolnir"]

        off = (await client.put("/api/apps/mjolnir/autostart", json={"autostart": False})).json()
        assert off["autostart"] is False
        assert (await extensions.settings()).autostart == []
    await supervisor.stop()


@pytest.mark.asyncio
async def test_unknown_app_is_404(env: tuple[httpx.AsyncClient, AppSupervisor]) -> None:
    client, _ = env
    assert (await client.post("/api/apps/nope/start")).status_code == 404


@pytest.mark.asyncio
async def test_reads_need_the_token(tmp_path: Path) -> None:
    api, supervisor, _ = _app(tmp_path)
    assert supervisor is not None
    async with _client(api, token=None) as anon:
        assert (await anon.get("/api/apps")).status_code == 401


@pytest.mark.asyncio
async def test_lifecycle_commands_are_refused_on_an_open_server(tmp_path: Path) -> None:
    # Starting a process is a state change on the machine; an unauthenticated
    # server offers reads only.
    api, supervisor, _ = _app(tmp_path, api_token=None)
    assert supervisor is not None
    await supervisor.start()
    async with _client(api, token=None) as anon:
        assert (await anon.get("/api/apps")).status_code == 200
        for resp in (
            await anon.post("/api/apps/mjolnir/start"),
            await anon.post("/api/apps/mjolnir/stop"),
            await anon.post("/api/apps/mjolnir/restart"),
            await anon.put("/api/apps/mjolnir/autostart", json={"autostart": True}),
        ):
            assert resp.status_code == 403, resp.request.url
    await supervisor.stop()


@pytest.mark.asyncio
async def test_unwired_supervisor_reports_unavailable(tmp_path: Path) -> None:
    api, _, _ = _app(tmp_path, wire=False)
    async with _client(api) as client:
        assert (await client.get("/api/apps")).status_code == 503


# --- setup readiness (ADR-0017) ----------------------------------------------


class _Gap:
    description = "Download the voice (~63 MB) in the setup wizard"
    config_pointer = "engines.piper.voice_path"


def _gapped_app(tmp_path: Path) -> tuple[FastAPI, AppSupervisor]:
    """An app whose supervisor reports one unmet setup step."""
    bus = InProcessEventBus()
    registry = SessionRegistry(bus)
    mediation = MediationService(bus)
    manager = AdapterManager(bus, registry, mediation, data_dir=tmp_path, discovery_interval=0)

    async def config_fn(_name: str) -> dict[str, Any]:
        return {}

    async def gaps_fn(_name: str) -> list[_Gap]:
        return [_Gap()]

    supervisor = AppSupervisor(
        config_fn=config_fn,
        manifests_fn=lambda: [_manifest()],
        spawn_fn=_spawn,
        setup_gaps_fn=gaps_fn,
    )
    api = create_app(
        registry=registry,
        store=SqliteEventStore(tmp_path / "events.db"),
        bus=bus,
        mediation=mediation,
        manager=manager,
        scheduler=SchedulerService(bus, manager, node="test-node"),
        presence=PresenceTracker(),
        node="test-node",
        version="0.0-test",
        apps=supervisor,
        api_token=TOKEN,
    )
    return api, supervisor


@pytest.mark.asyncio
async def test_listing_carries_the_unmet_setup_steps(tmp_path: Path) -> None:
    api, supervisor = _gapped_app(tmp_path)
    await supervisor.start()
    try:
        async with _client(api) as client:
            (app,) = (await client.get("/api/apps")).json()["apps"]
    finally:
        await supervisor.stop()

    assert app["unmet_setup"] == ["Download the voice (~63 MB) in the setup wizard"]


@pytest.mark.asyncio
async def test_starting_an_unready_app_is_412_with_the_steps(tmp_path: Path) -> None:
    api, supervisor = _gapped_app(tmp_path)
    await supervisor.start()
    try:
        async with _client(api) as client:
            resp = await client.post("/api/apps/mjolnir/start")
    finally:
        await supervisor.stop()

    assert resp.status_code == 412
    detail = resp.json()["detail"]
    assert "not ready to start" in detail
    assert "Download the voice" in detail
