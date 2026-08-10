"""ExtensionService: inventory presentation and config precedence."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from prodeo.errors import NotEntitledError, UnknownExtensionError
from prodeo.events import Event
from prodeo.extensions import (
    Catalog,
    CatalogEntry,
    ExtensionService,
    InstallResult,
    JsonFileConfigStore,
)
from prodeo.plugins import ExtensionInfo, PluginManifest


class DemoConfig(BaseModel):
    base_url: str = "http://127.0.0.1:11434"
    model: str
    timeout_s: float = 60.0


def _manifest(name: str = "ollama", kind: Any = "summarizer", **extra: Any) -> PluginManifest:
    return PluginManifest(
        name=name,
        kind=kind,
        version="0.1.0",
        factory=lambda config: config,
        config_model=DemoConfig,
        **extra,
    )


def _service(
    tmp_path: Path,
    inventory: list[ExtensionInfo],
    env: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> ExtensionService:
    return ExtensionService(
        inventory_fn=lambda: inventory,
        env_config=env or {},
        store=JsonFileConfigStore(tmp_path / "extensions.json"),
    )


def test_list_exposes_manifest_metadata(tmp_path: Path) -> None:
    manifest = _manifest(
        description="Local LLM prose",
        publisher="Prodeo",
        license="Apache-2.0",
        categories=["summarizer"],
    )
    svc = _service(tmp_path, [ExtensionInfo("ollama", "loaded", manifest=manifest)])

    (summary,) = svc.list()
    assert (summary.name, summary.kind, summary.status) == ("ollama", "summarizer", "loaded")
    assert summary.description == "Local LLM prose"
    assert summary.license == "Apache-2.0"
    assert summary.configurable is True
    assert summary.hosted_by_client is False


def test_voice_kinds_are_listed_as_hosted_by_client(tmp_path: Path) -> None:
    # The server skips voice engines, but the manager must still show them:
    # they are installed, just loaded by the mjolnir process.
    info = ExtensionInfo("piper", "hosted_by_client", manifest=_manifest("piper", "tts"))
    (summary,) = _service(tmp_path, [info]).list()
    assert (summary.status, summary.hosted_by_client) == ("hosted_by_client", True)


def test_failed_entry_point_is_listed_with_its_error(tmp_path: Path) -> None:
    # No manifest resolved, so the entry point name is all we can show.
    info = ExtensionInfo("broken", "failed", error="boom")
    (summary,) = _service(tmp_path, [info]).list()
    assert (summary.name, summary.status, summary.error) == ("broken", "failed", "boom")
    assert summary.kind is None and summary.configurable is False


def test_get_returns_the_config_schema(tmp_path: Path) -> None:
    svc = _service(tmp_path, [ExtensionInfo("ollama", "loaded", manifest=_manifest())])
    detail = svc.get("ollama")
    assert detail.config_schema is not None
    assert set(detail.config_schema["properties"]) == {"base_url", "model", "timeout_s"}


def test_unknown_extension_raises(tmp_path: Path) -> None:
    with pytest.raises(UnknownExtensionError, match="nope"):
        _service(tmp_path, []).get("nope")


@pytest.mark.asyncio
async def test_saved_config_overlays_environment_per_key(tmp_path: Path) -> None:
    svc = _service(
        tmp_path,
        [ExtensionInfo("ollama", "loaded", manifest=_manifest())],
        env={"summarizer": {"ollama": {"model": "from-env", "timeout_s": 30.0}}},
    )
    await svc.set_config("ollama", {"model": "from-ui"})

    config = await svc.config("ollama")
    assert config.values == {"model": "from-ui", "timeout_s": 30.0}
    # Per-key provenance: the UI owns what it wrote, the env keeps the rest.
    assert config.sources == {"model": "saved", "timeout_s": "environment"}


@pytest.mark.asyncio
async def test_set_config_validates_the_merged_result_not_the_overlay(tmp_path: Path) -> None:
    # `model` is required and comes from the environment. Editing only
    # `timeout_s` must succeed - validating the overlay alone would fail.
    svc = _service(
        tmp_path,
        [ExtensionInfo("ollama", "loaded", manifest=_manifest())],
        env={"summarizer": {"ollama": {"model": "from-env"}}},
    )
    saved = await svc.set_config("ollama", {"timeout_s": 5.0})
    assert saved.values["model"] == "from-env"
    assert saved.restart_required is True


@pytest.mark.asyncio
async def test_invalid_config_is_rejected_and_not_persisted(tmp_path: Path) -> None:
    svc = _service(
        tmp_path,
        [ExtensionInfo("ollama", "loaded", manifest=_manifest())],
        env={"summarizer": {"ollama": {"model": "from-env"}}},
    )
    with pytest.raises(ValidationError):
        await svc.set_config("ollama", {"timeout_s": "not-a-number"})

    assert (await svc.config("ollama")).values == {"model": "from-env"}


@pytest.mark.asyncio
async def test_config_without_saved_overlay_is_all_environment(tmp_path: Path) -> None:
    svc = _service(
        tmp_path,
        [ExtensionInfo("ollama", "loaded", manifest=_manifest())],
        env={"summarizer": {"ollama": {"model": "from-env"}}},
    )
    config = await svc.config("ollama")
    assert config.sources == {"model": "environment"}
    assert config.restart_required is False


# --- installation and enablement -------------------------------------------


class FakeInstaller:
    """Records what it was asked to install; can be told to fail."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.installed: list[str] = []
        self.uninstalled: list[str] = []

    async def install(self, package: str) -> InstallResult:
        self.installed.append(package)
        return InstallResult(ok=self.ok, package=package, error="" if self.ok else "boom")

    async def uninstall(self, package: str) -> InstallResult:
        self.uninstalled.append(package)
        return InstallResult(ok=self.ok, package=package, error="" if self.ok else "boom")


class FakeCatalog:
    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries = entries

    async def fetch(self) -> Catalog:
        return Catalog(source="fake", entries=self._entries)


def _installable_service(
    tmp_path: Path, installer: FakeInstaller, events: list[Event] | None = None
) -> ExtensionService:
    sink = events if events is not None else []

    async def publish(event: Event) -> None:
        sink.append(event)

    return ExtensionService(
        inventory_fn=lambda: [ExtensionInfo("ollama", "loaded", manifest=_manifest())],
        env_config={},
        store=JsonFileConfigStore(tmp_path / "extensions.json"),
        catalog=FakeCatalog(
            [CatalogEntry(name="ollama", package="prodeo-summarizer-ollama", version="0.1.0")]
        ),
        installer=installer,
        publish=publish,
    )


@pytest.mark.asyncio
async def test_install_resolves_the_package_from_the_catalog(tmp_path: Path) -> None:
    # The caller passes a name, never a package spec - that is what stops this
    # endpoint being a general-purpose "run arbitrary code" API.
    installer = FakeInstaller()
    events: list[Event] = []
    svc = _installable_service(tmp_path, installer, events)

    result = await svc.install("ollama")

    assert result.ok is True
    assert installer.installed == ["prodeo-summarizer-ollama"]
    assert [e.type for e in events] == ["system.extension_installed"]
    assert events[0].payload["package"] == "prodeo-summarizer-ollama"


@pytest.mark.asyncio
async def test_installing_something_not_in_the_catalog_is_refused(tmp_path: Path) -> None:
    installer = FakeInstaller()
    svc = _installable_service(tmp_path, installer)

    with pytest.raises(UnknownExtensionError, match="not in the extension catalog"):
        await svc.install("totally-made-up")

    assert installer.installed == []  # never reached the installer


@pytest.mark.asyncio
async def test_failed_install_emits_the_failure_event(tmp_path: Path) -> None:
    events: list[Event] = []
    svc = _installable_service(tmp_path, FakeInstaller(ok=False), events)

    result = await svc.install("ollama")

    assert result.ok is False
    assert [e.type for e in events] == ["system.extension_install_failed"]
    assert events[0].payload["error"] == "boom"


@pytest.mark.asyncio
async def test_uninstall_clears_saved_state(tmp_path: Path) -> None:
    installer = FakeInstaller()
    events: list[Event] = []
    svc = _installable_service(tmp_path, installer, events)
    await svc.set_config("ollama", {"model": "llama3.2"})
    await svc.set_enabled("ollama", False)

    result = await svc.uninstall("ollama")

    assert result.ok is True and installer.uninstalled == ["prodeo-summarizer-ollama"]
    # A reinstall should start clean, not inherit config for a version that
    # may no longer have those fields.
    assert (await svc.config("ollama")).values == {}
    assert "system.extension_uninstalled" in [e.type for e in events]


@pytest.mark.asyncio
async def test_failed_uninstall_keeps_saved_state(tmp_path: Path) -> None:
    svc = _installable_service(tmp_path, FakeInstaller(ok=False))
    await svc.set_config("ollama", {"model": "llama3.2"})

    result = await svc.uninstall("ollama")

    assert result.ok is False
    assert (await svc.config("ollama")).values == {"model": "llama3.2"}


@pytest.mark.asyncio
async def test_set_enabled_persists_and_reports_disabled(tmp_path: Path) -> None:
    svc = _installable_service(tmp_path, FakeInstaller())

    summary = await svc.set_enabled("ollama", False)

    assert summary.status == "disabled"
    assert await svc._store.disabled() == {"ollama"}
    assert (await svc.set_enabled("ollama", True)).status == "loaded"


@pytest.mark.asyncio
async def test_install_without_an_installer_is_refused(tmp_path: Path) -> None:
    svc = _service(tmp_path, [ExtensionInfo("ollama", "loaded", manifest=_manifest())])
    with pytest.raises(UnknownExtensionError):
        await svc.install("ollama")


@pytest.mark.asyncio
async def test_settings_round_trip(tmp_path: Path) -> None:
    svc = _installable_service(tmp_path, FakeInstaller())
    settings = await svc.settings()
    settings.models_dir = str(tmp_path / "models")
    saved = await svc.set_settings(settings)
    assert saved.models_dir == str(tmp_path / "models")


def _paid_service(tmp_path: Path, installer: FakeInstaller) -> ExtensionService:
    return ExtensionService(
        inventory_fn=lambda: [],
        env_config={},
        store=JsonFileConfigStore(tmp_path / "extensions.json"),
        catalog=FakeCatalog(
            [
                CatalogEntry(
                    name="mjolnir",
                    package="prodeo-mjolnir[audio]",
                    tier="paid",
                    tier_note="Mjolnir is a paid extension.",
                )
            ]
        ),
        installer=installer,
    )


@pytest.mark.asyncio
async def test_paid_extension_needs_a_licence_key(tmp_path: Path) -> None:
    installer = FakeInstaller()
    svc = _paid_service(tmp_path, installer)

    with pytest.raises(NotEntitledError, match="paid extension"):
        await svc.install("mjolnir")

    assert installer.installed == []  # never reached the installer


@pytest.mark.asyncio
async def test_paid_extension_installs_once_entitled(tmp_path: Path) -> None:
    installer = FakeInstaller()
    svc = _paid_service(tmp_path, installer)
    settings = await svc.settings()
    settings.license_key = "test-key"
    await svc.set_settings(settings)

    result = await svc.install("mjolnir")

    assert result.ok is True
    assert installer.installed == ["prodeo-mjolnir[audio]"]


@pytest.mark.asyncio
async def test_whitespace_is_not_a_licence_key(tmp_path: Path) -> None:
    svc = _paid_service(tmp_path, FakeInstaller())
    settings = await svc.settings()
    settings.license_key = "   "
    await svc.set_settings(settings)

    with pytest.raises(NotEntitledError):
        await svc.install("mjolnir")


def test_inventory_is_read_lazily(tmp_path: Path) -> None:
    # The composition root wires the service before the host has loaded, so a
    # snapshot taken at construction time would always be empty.
    inventory: list[ExtensionInfo] = []
    svc = _service(tmp_path, inventory)
    assert svc.list() == []
    inventory.append(ExtensionInfo("ollama", "loaded", manifest=_manifest()))
    assert [s.name for s in svc.list()] == ["ollama"]
