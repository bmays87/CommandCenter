"""The /api/machines surface: catalogue reads, rename/remove writes, the
pairing handshake behind Add Machine, and installer listing/download
(ADR-0024, ADR-0025)."""

import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from prodeo.adapters import AdapterManager
from prodeo.api import create_app
from prodeo.bus import InProcessEventBus
from prodeo.errors import PairingError
from prodeo.identity import IdentityProvider
from prodeo.machines import MachineRegistry
from prodeo.machines.enrollments import Enrollments
from prodeo.machines.installers import InstallerBuilder
from prodeo.machines.pairing import PairedCcan
from prodeo.mediation import MediationService
from prodeo.persistence import SqliteEventStore
from prodeo.presence import PresenceTracker
from prodeo.scheduler import SchedulerService
from prodeo.sessions import SessionRegistry

TOKEN = "secret-token"


class FakePairing:
    """Answers like a paired CCAN, or fails like an unreachable one."""

    def __init__(self, *, fail: str = "") -> None:
        self.fail = fail
        self.addresses: list[str] = []

    async def pair(self, address: str) -> PairedCcan:
        self.addresses.append(address)
        if self.fail:
            raise PairingError(self.fail)
        return PairedCcan(node="worker-01", name="Worker", certificate_pem="PEM")


def _installers(tmp_path: Path) -> InstallerBuilder:
    wheels = tmp_path / "wheels"
    wheels.mkdir(exist_ok=True)
    (wheels / "prodeo-0.1.0-py3-none-any.whl").write_bytes(b"core")
    (wheels / "prodeo_ccan-0.1.0-py3-none-any.whl").write_bytes(b"ccan")
    return InstallerBuilder(
        workspace=tmp_path,
        wheels_dir=wheels,
        out_dir=tmp_path / "out",
        identity=IdentityProvider(tmp_path / "identity", common_name="test-node"),
        enrollments=Enrollments(tmp_path / "enrollments.json"),
        hub_node="test-node",
    )


def _app(
    tmp_path: Path,
    *,
    api_token: str | None = TOKEN,
    wire_machines: bool = True,
    pairing: FakePairing | None = None,
    installers: InstallerBuilder | None = None,
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
        pairing=pairing,
        installers=installers,
    )
    return app, machines


def _client(app: FastAPI, *, token: str | None = TOKEN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
    )


# --- catalogue ---------------------------------------------------------------


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


# --- pairing (Add Machine) ---------------------------------------------------


@pytest.mark.asyncio
async def test_add_machine_pairs_and_registers(tmp_path: Path) -> None:
    pairing = FakePairing()
    app, machines = _app(tmp_path, pairing=pairing)

    async with _client(app) as client:
        resp = await client.post("/api/machines", json={"address": "worker-01.lan"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["node"] == "worker-01"
    assert body["name"] == "Worker"
    assert body["address"] == "worker-01.lan"
    assert body["certificate"] == "PEM"
    assert pairing.addresses == ["worker-01.lan"]
    assert machines.get_by_node("worker-01") is not None


@pytest.mark.asyncio
async def test_add_machine_maps_pairing_failure_to_502(tmp_path: Path) -> None:
    app, machines = _app(tmp_path, pairing=FakePairing(fail="no CCAN answered at a.lan"))

    async with _client(app) as client:
        resp = await client.post("/api/machines", json={"address": "a.lan"})

    assert resp.status_code == 502
    assert "no CCAN answered" in resp.json()["detail"]
    assert machines.list_machines() == []


@pytest.mark.asyncio
async def test_add_machine_twice_is_409(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, pairing=FakePairing())
    async with _client(app) as client:
        assert (await client.post("/api/machines", json={"address": "a.lan"})).status_code == 201
        resp = await client.post("/api/machines", json={"address": "a.lan"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_add_machine_without_pairing_wired_is_503(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with _client(app) as client:
        resp = await client.post("/api/machines", json={"address": "a.lan"})
    assert resp.status_code == 503


# --- installers --------------------------------------------------------------


@pytest.mark.asyncio
async def test_installer_list_offers_the_platform_agnostic_build(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, installers=_installers(tmp_path))
    async with _client(app) as client:
        body = (await client.get("/api/ccan/installers")).json()

    assert [i["platform"] for i in body["installers"]] == ["any"]
    assert body["installers"][0]["url"] == "/api/ccan/installers/any/download"


@pytest.mark.asyncio
async def test_installer_list_explains_when_unavailable(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)  # no builder wired
    async with _client(app) as client:
        body = (await client.get("/api/ccan/installers")).json()
    assert body["installers"] == []
    assert body["note"]


@pytest.mark.asyncio
async def test_installer_download_streams_a_fresh_zip(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, installers=_installers(tmp_path))
    async with _client(app) as client:
        resp = await client.get("/api/ccan/installers/any/download")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        assert "install.py" in zf.namelist()
        assert "ccan.json" in zf.namelist()


# --- gating ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_machine_writes_are_refused_on_an_open_server(tmp_path: Path) -> None:
    app, machines = _app(
        tmp_path, api_token=None, pairing=FakePairing(), installers=_installers(tmp_path)
    )
    worker = await machines.add(node="worker-01", address="a.lan")

    async with _client(app, token=None) as anon:
        assert (await anon.get("/api/machines")).status_code == 200
        for resp in (
            await anon.post("/api/machines", json={"address": "b.lan"}),
            await anon.put(f"/api/machines/{worker.id}/name", json={"name": "x"}),
            await anon.delete(f"/api/machines/{worker.id}"),
            await anon.get("/api/ccan/installers/any/download"),
        ):
            assert resp.status_code == 403, resp.request.url
            assert "PRODEO_API_TOKEN" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_machines_report_when_unwired(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, wire_machines=False)
    async with _client(app) as client:
        resp = await client.get("/api/machines")
    assert resp.status_code == 503
