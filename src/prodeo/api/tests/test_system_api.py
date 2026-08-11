"""The /api/system surface: restarting the server and browsing for a path.

Both are token-gated writes rather than ordinary reads (ADR-0016), so the
"refused on an open server" case is as much the contract as the happy path.
"""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from prodeo.adapters import AdapterManager
from prodeo.api import create_app
from prodeo.api.app import _list_directories
from prodeo.bus import InProcessEventBus
from prodeo.mediation import MediationService
from prodeo.persistence import SqliteEventStore
from prodeo.presence import PresenceTracker
from prodeo.scheduler import SchedulerService
from prodeo.sessions import SessionRegistry

TOKEN = "secret-token"


class FakeMachine:
    """Records editor-open requests; raises what the test scripts."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.opened: list[str] = []
        self.fail_with = fail_with

    async def open_editor(self, path: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.opened.append(path)


def _app(
    tmp_path: Path,
    *,
    api_token: str | None = TOKEN,
    restarts: list[int] | None = None,
    machine: FakeMachine | None = None,
) -> FastAPI:
    bus = InProcessEventBus()
    registry = SessionRegistry(bus)
    mediation = MediationService(bus)
    manager = AdapterManager(bus, registry, mediation, data_dir=tmp_path, discovery_interval=0)
    return create_app(
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
        restart_fn=None if restarts is None else lambda: restarts.append(1),
        machine=machine,
    )


def _client(app: FastAPI, *, token: str | None = TOKEN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
    )


# --- restart -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_carries_a_boot_id(tmp_path: Path) -> None:
    # Without it a client cannot tell "restarted" from "never went down".
    async with _client(_app(tmp_path)) as client:
        body = (await client.get("/api/health")).json()
    assert body["boot_id"]


@pytest.mark.asyncio
async def test_restart_acknowledges_before_it_restarts(tmp_path: Path) -> None:
    calls: list[int] = []
    async with _client(_app(tmp_path, restarts=calls)) as client:
        resp = await client.post("/api/system/restart")

    assert resp.status_code == 200
    body = resp.json()
    assert body["restarting"] is True
    # The response names the boot being replaced, which is what the dashboard
    # polls against; the shutdown itself runs after the response is flushed.
    assert body["boot_id"]
    assert calls == [1]


@pytest.mark.asyncio
async def test_restart_is_refused_without_a_token(tmp_path: Path) -> None:
    calls: list[int] = []
    async with _client(_app(tmp_path, api_token=None, restarts=calls), token=None) as client:
        resp = await client.post("/api/system/restart")

    assert resp.status_code == 403
    assert calls == []  # never reached the server


@pytest.mark.asyncio
async def test_restart_reports_when_it_is_not_wired(tmp_path: Path) -> None:
    async with _client(_app(tmp_path)) as client:
        resp = await client.post("/api/system/restart")
    assert resp.status_code == 503


# --- browse ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_lists_only_directories(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "voices").mkdir()
    (tmp_path / "notes.txt").write_text("not a directory", encoding="utf-8")

    async with _client(_app(tmp_path)) as client:
        body = (await client.get("/api/system/browse", params={"path": str(tmp_path)})).json()

    assert [e["name"] for e in body["entries"]] == ["models", "voices"]
    assert body["path"] == str(tmp_path.resolve())
    assert body["parent"] == str(tmp_path.resolve().parent)


@pytest.mark.asyncio
async def test_browse_with_no_path_lists_roots(tmp_path: Path) -> None:
    async with _client(_app(tmp_path)) as client:
        body = (await client.get("/api/system/browse")).json()

    # Drives on Windows, "/" elsewhere - either way there is somewhere to start
    # and no parent to go up to.
    assert body["entries"]
    assert body["parent"] is None


@pytest.mark.asyncio
async def test_browse_rejects_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")

    async with _client(_app(tmp_path)) as client:
        resp = await client.get("/api/system/browse", params={"path": str(target)})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_browse_is_refused_without_a_token(tmp_path: Path) -> None:
    # Enumerating the filesystem is a disclosure an open server must not offer.
    async with _client(_app(tmp_path, api_token=None), token=None) as client:
        resp = await client.get("/api/system/browse", params={"path": str(tmp_path)})

    assert resp.status_code == 403


def test_browse_skips_an_unreadable_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One C:\\System Volume Information must not fail the whole listing."""
    (tmp_path / "good").mkdir()
    (tmp_path / "bad").mkdir()
    real_is_dir = Path.is_dir

    def flaky_is_dir(self: Path) -> bool:
        if self.name == "bad":
            raise PermissionError(self.name)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", flaky_is_dir)

    listing = _list_directories(str(tmp_path))

    assert [e.name for e in listing.entries] == ["good"]


# --- open editor (ADR-0020: through the MachineActions seam) -----------------


@pytest.mark.asyncio
async def test_open_editor_goes_through_the_machine_seam(tmp_path: Path) -> None:
    machine = FakeMachine()
    async with _client(_app(tmp_path, machine=machine)) as client:
        resp = await client.post("/api/system/open-editor", json={"path": str(tmp_path)})

    assert resp.status_code == 200
    assert resp.json() == {"opened": True, "path": str(tmp_path)}
    assert machine.opened == [str(tmp_path)]


@pytest.mark.asyncio
async def test_open_editor_maps_machine_errors_to_400_with_the_fix(tmp_path: Path) -> None:
    from prodeo.errors import MachineActionError

    machine = FakeMachine(fail_with=MachineActionError("VS Code is not on PATH"))
    async with _client(_app(tmp_path, machine=machine)) as client:
        resp = await client.post("/api/system/open-editor", json={"path": str(tmp_path)})

    assert resp.status_code == 400
    assert "not on PATH" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_open_editor_is_refused_without_a_token(tmp_path: Path) -> None:
    machine = FakeMachine()
    async with _client(_app(tmp_path, api_token=None, machine=machine), token=None) as client:
        resp = await client.post("/api/system/open-editor", json={"path": str(tmp_path)})

    assert resp.status_code == 403
    assert machine.opened == []


@pytest.mark.asyncio
async def test_open_editor_reports_when_unwired(tmp_path: Path) -> None:
    async with _client(_app(tmp_path)) as client:
        resp = await client.post("/api/system/open-editor", json={"path": str(tmp_path)})
    assert resp.status_code == 503
