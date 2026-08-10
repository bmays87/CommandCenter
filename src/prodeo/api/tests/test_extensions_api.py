"""The /api/extensions surface: inventory, catalog, and config read/write."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel

from prodeo.adapters import AdapterManager
from prodeo.api import create_app
from prodeo.bus import InProcessEventBus
from prodeo.extensions import (
    BundledCatalog,
    ExtensionService,
    InstallResult,
    JsonFileConfigStore,
)
from prodeo.mediation import MediationService
from prodeo.persistence import SqliteEventStore
from prodeo.plugins import ExtensionInfo, PluginManifest
from prodeo.presence import PresenceTracker
from prodeo.scheduler import SchedulerService
from prodeo.sessions import SessionRegistry

TOKEN = "secret-token"


class DemoConfig(BaseModel):
    model: str = "llama3.1:8b"
    timeout_s: float = 60.0


def _inventory() -> list[ExtensionInfo]:
    summarizer = PluginManifest(
        name="ollama",
        kind="summarizer",
        version="0.1.0",
        config_model=DemoConfig,
        factory=lambda cfg: cfg,
        description="Local LLM prose",
        publisher="Prodeo",
        license="Apache-2.0",
        categories=["summarizer"],
    )
    voice = PluginManifest(
        name="piper", kind="tts", version="0.1.0", factory=lambda cfg: cfg, license="GPL-3.0"
    )
    return [
        ExtensionInfo("ollama", "loaded", manifest=summarizer),
        ExtensionInfo("piper", "hosted_by_client", manifest=voice),
        ExtensionInfo("broken", "failed", error="missing dependency"),
    ]


class FakeInstaller:
    """Stands in for uv/pip so no test ever installs anything."""

    async def install(self, package: str) -> InstallResult:
        return InstallResult(ok=True, package=package, output="installed")

    async def uninstall(self, package: str) -> InstallResult:
        return InstallResult(ok=True, package=package, output="removed")


def _app(tmp_path: Path, *, api_token: str | None = TOKEN, wire_extensions: bool = True) -> FastAPI:
    bus = InProcessEventBus()
    registry = SessionRegistry(bus)
    mediation = MediationService(bus)
    manager = AdapterManager(bus, registry, mediation, data_dir=tmp_path, discovery_interval=0)
    extensions = (
        ExtensionService(
            inventory_fn=_inventory,
            env_config={"summarizer": {"ollama": {"model": "from-env"}}},
            store=JsonFileConfigStore(tmp_path / "extensions.json"),
            catalog=BundledCatalog(),
            installer=FakeInstaller(),
            publish=bus.publish,
        )
        if wire_extensions
        else None
    )
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
        extensions=extensions,
        catalog=BundledCatalog() if wire_extensions else None,
        api_token=api_token,
    )


def _client(app: FastAPI, *, token: str | None = TOKEN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
    )


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(_app(tmp_path)) as c:
        yield c


@pytest.mark.asyncio
async def test_lists_every_discovered_extension(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/extensions")).json()
    by_name = {e["name"]: e for e in body["extensions"]}

    assert by_name["ollama"]["kind"] == "summarizer"
    assert by_name["ollama"]["description"] == "Local LLM prose"
    assert by_name["ollama"]["configurable"] is True
    # Installed but loaded by the mjolnir process, not this one.
    assert by_name["piper"]["hosted_by_client"] is True
    # A plugin that failed to load is still inventory, with its reason.
    assert by_name["broken"]["status"] == "failed"
    assert by_name["broken"]["error"] == "missing dependency"


@pytest.mark.asyncio
async def test_detail_returns_the_config_schema(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/extensions/ollama")).json()
    assert set(body["config_schema"]["properties"]) == {"model", "timeout_s"}


@pytest.mark.asyncio
async def test_unknown_extension_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/extensions/nope")).status_code == 404


@pytest.mark.asyncio
async def test_catalog_is_served_and_not_shadowed_by_the_name_route(
    client: httpx.AsyncClient,
) -> None:
    # "/api/extensions/catalog" must not be parsed as an extension named
    # "catalog" - route registration order is what guarantees this.
    body = (await client.get("/api/extensions/catalog")).json()
    assert body["source"] == "bundled"
    names = {e["name"] for e in body["entries"]}
    assert {"mjolnir", "claude-code", "faster-whisper"} <= names
    mjolnir = next(e for e in body["entries"] if e["name"] == "mjolnir")
    assert mjolnir["extension_class"] == "app"  # a process, not an in-process plugin


@pytest.mark.asyncio
async def test_config_round_trip_and_precedence(client: httpx.AsyncClient) -> None:
    before = (await client.get("/api/extensions/ollama/config")).json()
    assert before["values"] == {"model": "from-env"}
    assert before["sources"] == {"model": "environment"}
    assert before["restart_required"] is False

    saved = await client.put("/api/extensions/ollama/config", json={"values": {"model": "from-ui"}})
    assert saved.status_code == 200
    assert saved.json()["values"]["model"] == "from-ui"
    assert saved.json()["sources"]["model"] == "saved"
    # The host loads once at boot; be honest that this is not live.
    assert saved.json()["restart_required"] is True

    after = (await client.get("/api/extensions/ollama/config")).json()
    assert after["values"]["model"] == "from-ui"


@pytest.mark.asyncio
async def test_invalid_config_is_422_and_not_persisted(client: httpx.AsyncClient) -> None:
    resp = await client.put(
        "/api/extensions/ollama/config", json={"values": {"timeout_s": "not-a-number"}}
    )
    assert resp.status_code == 422
    assert (await client.get("/api/extensions/ollama/config")).json()["values"] == {
        "model": "from-env"
    }


@pytest.mark.asyncio
async def test_extension_routes_require_the_token(tmp_path: Path) -> None:
    async with _client(_app(tmp_path), token=None) as anon:
        assert (await anon.get("/api/extensions")).status_code == 401


@pytest.mark.asyncio
async def test_config_write_is_refused_on_an_open_server(tmp_path: Path) -> None:
    # Config values become plugin constructor arguments. An unauthenticated
    # server must not offer that, even though it happily serves reads.
    async with _client(_app(tmp_path, api_token=None), token=None) as anon:
        assert (await anon.get("/api/extensions")).status_code == 200
        resp = await anon.put("/api/extensions/ollama/config", json={"values": {"model": "x"}})
        assert resp.status_code == 403
        assert "PRODEO_API_TOKEN" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_unwired_manager_reports_unavailable(tmp_path: Path) -> None:
    async with _client(_app(tmp_path, wire_extensions=False)) as c:
        assert (await c.get("/api/extensions")).status_code == 503
        assert (await c.get("/api/extensions/catalog")).status_code == 503


# --- install / uninstall / enable -------------------------------------------


@pytest.mark.asyncio
async def test_install_resolves_a_catalog_name(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/extensions/ollama/install")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # The package spec came from the catalog, not from the request.
    assert body["package"] == "prodeo-summarizer-ollama"


@pytest.mark.asyncio
async def test_installing_a_name_not_in_the_catalog_is_404(client: httpx.AsyncClient) -> None:
    # The endpoint takes a catalog name, never a package spec, so it cannot be
    # used to install arbitrary code.
    resp = await client.post("/api/extensions/some-attacker-package/install")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_enable_disable_round_trip(client: httpx.AsyncClient) -> None:
    off = await client.put("/api/extensions/ollama/enabled", json={"enabled": False})
    assert off.status_code == 200 and off.json()["status"] == "disabled"

    on = await client.put("/api/extensions/ollama/enabled", json={"enabled": True})
    assert on.json()["status"] == "loaded"


@pytest.mark.asyncio
async def test_extension_settings_round_trip(client: httpx.AsyncClient, tmp_path: Path) -> None:
    assert (await client.get("/api/extension-settings")).json()["models_dir"] == ""

    saved = await client.put(
        "/api/extension-settings", json={"models_dir": str(tmp_path / "models"), "autostart": []}
    )
    assert saved.status_code == 200
    assert saved.json()["models_dir"] == str(tmp_path / "models")


@pytest.mark.asyncio
async def test_every_state_changing_route_is_refused_on_an_open_server(tmp_path: Path) -> None:
    # Installing executes code; config values become constructor arguments.
    # An unauthenticated server must offer none of it, while reads still work.
    async with _client(_app(tmp_path, api_token=None), token=None) as anon:
        assert (await anon.get("/api/extensions")).status_code == 200
        for resp in (
            await anon.post("/api/extensions/ollama/install"),
            await anon.request("DELETE", "/api/extensions/ollama/install"),
            await anon.put("/api/extensions/ollama/enabled", json={"enabled": False}),
            await anon.put("/api/extension-settings", json={"models_dir": "x", "autostart": []}),
            await anon.put("/api/extensions/ollama/config", json={"values": {}}),
        ):
            assert resp.status_code == 403, resp.request.url
            assert "PRODEO_API_TOKEN" in resp.json()["detail"]
