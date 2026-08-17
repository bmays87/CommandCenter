"""The /api/machines surface: catalogue reads, rename/remove writes, and the
honest 501 for pairing plus the empty installer list until the CCAN package
lands (Phase 6 workstream B)."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from prodeo.adapters import AdapterManager
from prodeo.api import create_app
from prodeo.bus import InProcessEventBus
from prodeo.machines import MachineRegistry
from prodeo.mediation import MediationService
from prodeo.persistence import SqliteEventStore
from prodeo.presence import PresenceTracker
from prodeo.scheduler import SchedulerService
from prodeo.sessions import SessionRegistry

TOKEN = "secret-token"


def _app(
    tmp_path: Path,
    *,
    api_token: str | None = TOKEN,
    wire_machines: bool = True,
) -> tuple[FastAPI, MachineRegistry]:
    bus = InProcessEventBus()
    registry = SessionRegistry(bus)
    mediation = MediationService(bus)
    manager = AdapterManager(bus, registry, mediation, data_dir=tmp_path, discovery_interval=0)
    machines = MachineRegistry(bus, node="test-node")
    app = create_app(
        registry=registry,
        store=SqliteEventStore(tmp_path / "events.db"),  # never opened; unused here
        bus=bus,
        mediation=mediation,
        manager=manager,
        scheduler=SchedulerService(bus, manager, node="test-node"),
        presence=PresenceTracker(),
        node="test-node",
        version="0.0-test",
        api_token=api_token,
        machines=machines if wire_machines else None,
    )
    return app, machines


def _client(app: FastAPI, *, token: str | None = TOKEN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
    )


@pytest.mark.asyncio
async def test_list_machines_in_tab_order(tmp_path: Path) -> None:
    app, machines = _app(tmp_path)
    local = await machines.ensure_local()
    worker = await machines.add(node="worker-01", address="worker-01.lan")

    async with _client(app) as client:
        body = (await client.get("/api/machines")).json()

    assert [m["id"] for m in body["machines"]] == [local.id, worker.id]
    assert body["machines"][0]["node"] == "test-node"
    assert body["machines"][0]["address"] is None


@pytest.mark.asyncio
async def test_add_machine_is_honest_about_missing_pairing(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with _client(app) as client:
        resp = await client.post("/api/machines", json={"address": "worker-01.lan"})

    assert resp.status_code == 501
    assert "CCAN" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rename_machine_changes_the_tab_name(tmp_path: Path) -> None:
    app, machines = _app(tmp_path)
    worker = await machines.add(node="worker-01", address="a.lan")

    async with _client(app) as client:
        resp = await client.put(f"/api/machines/{worker.id}/name", json={"name": "GPU box"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "GPU box"
    assert resp.json()["node"] == "worker-01"


@pytest.mark.asyncio
async def test_rename_unknown_machine_is_404(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with _client(app) as client:
        resp = await client.put("/api/machines/nope/name", json={"name": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_machine(tmp_path: Path) -> None:
    app, machines = _app(tmp_path)
    worker = await machines.add(node="worker-01", address="a.lan")

    async with _client(app) as client:
        resp = await client.delete(f"/api/machines/{worker.id}")

    assert resp.status_code == 204
    assert machines.get(worker.id) is None


@pytest.mark.asyncio
async def test_removing_the_hubs_own_machine_is_409(tmp_path: Path) -> None:
    app, machines = _app(tmp_path)
    local = await machines.ensure_local()

    async with _client(app) as client:
        resp = await client.delete(f"/api/machines/{local.id}")

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_installer_list_is_empty_with_an_explanation(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with _client(app) as client:
        body = (await client.get("/api/ccan/installers")).json()

    assert body["installers"] == []
    assert body["note"]


@pytest.mark.asyncio
async def test_machine_writes_are_refused_on_an_open_server(tmp_path: Path) -> None:
    app, machines = _app(tmp_path, api_token=None)
    worker = await machines.add(node="worker-01", address="a.lan")

    async with _client(app, token=None) as anon:
        assert (await anon.get("/api/machines")).status_code == 200
        for resp in (
            await anon.post("/api/machines", json={"address": "b.lan"}),
            await anon.put(f"/api/machines/{worker.id}/name", json={"name": "x"}),
            await anon.delete(f"/api/machines/{worker.id}"),
        ):
            assert resp.status_code == 403, resp.request.url
            assert "PRODEO_API_TOKEN" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_machines_report_when_unwired(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, wire_machines=False)
    async with _client(app) as client:
        resp = await client.get("/api/machines")
    assert resp.status_code == 503
