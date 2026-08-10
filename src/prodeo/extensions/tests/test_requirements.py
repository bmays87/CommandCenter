"""Catalog requirement evaluation, and that it blocks before downloading."""

from pathlib import Path

import pytest

from prodeo.environment import Environment
from prodeo.errors import RequirementsNotMetError
from prodeo.extensions import (
    BundledCatalog,
    Catalog,
    CatalogEntry,
    ExtensionRequirements,
    ExtensionService,
    InstallResult,
    JsonFileConfigStore,
    unmet_requirements,
)


def _env(**overrides: object) -> Environment:
    base: dict[str, object] = {"platform": "linux", "python": (3, 12), "nvidia_gpu": False}
    base.update(overrides)
    return Environment.model_validate(base)


def _entry(**requires: object) -> CatalogEntry:
    return CatalogEntry(
        name="parakeet",
        package="prodeo-stt-parakeet",
        requires=ExtensionRequirements.model_validate(requires),
    )


def test_no_requirements_is_always_met() -> None:
    assert unmet_requirements(CatalogEntry(name="x", package="x"), _env()) == []


def test_gpu_requirement_reports_a_reason() -> None:
    (reason,) = unmet_requirements(_entry(gpu=True), _env(nvidia_gpu=False))
    assert "NVIDIA GPU" in reason
    assert unmet_requirements(_entry(gpu=True), _env(nvidia_gpu=True)) == []


def test_python_floor_names_both_versions() -> None:
    (reason,) = unmet_requirements(_entry(min_python="3.13"), _env(python=(3, 12)))
    assert "3.13" in reason and "3.12" in reason


def test_platform_requirement() -> None:
    (reason,) = unmet_requirements(_entry(platforms=["win32"]), _env(platform="linux"))
    assert "win32" in reason and "linux" in reason
    assert unmet_requirements(_entry(platforms=["win32", "linux"]), _env()) == []


def test_all_failures_are_reported_together() -> None:
    # One round trip should tell the user everything that is wrong, not just
    # the first thing.
    reasons = unmet_requirements(_entry(gpu=True, min_python="3.13", platforms=["darwin"]), _env())
    assert len(reasons) == 3


class RecordingInstaller:
    def __init__(self) -> None:
        self.installed: list[str] = []

    async def install(self, package: str) -> InstallResult:
        self.installed.append(package)
        return InstallResult(ok=True, package=package)

    async def uninstall(self, package: str) -> InstallResult:
        return InstallResult(ok=True, package=package)


class FakeCatalog:
    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries = entries

    async def fetch(self) -> Catalog:
        return Catalog(source="fake", entries=self._entries)


def _service(tmp_path: Path, env: Environment, installer: RecordingInstaller) -> ExtensionService:
    return ExtensionService(
        inventory_fn=lambda: [],
        env_config={},
        store=JsonFileConfigStore(tmp_path / "extensions.json"),
        catalog=FakeCatalog([_entry(gpu=True)]),
        installer=installer,
        env=env,
    )


@pytest.mark.asyncio
async def test_install_is_refused_before_anything_downloads(tmp_path: Path) -> None:
    installer = RecordingInstaller()
    svc = _service(tmp_path, _env(nvidia_gpu=False), installer)

    with pytest.raises(RequirementsNotMetError, match="NVIDIA GPU"):
        await svc.install("parakeet")

    # The point of the gate: ~250MB is not downloaded to learn this.
    assert installer.installed == []


@pytest.mark.asyncio
async def test_install_proceeds_when_requirements_are_met(tmp_path: Path) -> None:
    installer = RecordingInstaller()
    svc = _service(tmp_path, _env(nvidia_gpu=True), installer)

    result = await svc.install("parakeet")

    assert result.ok is True
    assert installer.installed == ["prodeo-stt-parakeet"]


@pytest.mark.asyncio
async def test_catalog_annotates_entries_for_this_machine(tmp_path: Path) -> None:
    svc = _service(tmp_path, _env(nvidia_gpu=False), RecordingInstaller())
    catalog = await svc.catalog()
    assert catalog.entries[0].unmet == ["needs an NVIDIA GPU; none detected"]


@pytest.mark.asyncio
async def test_no_bundled_entry_demands_a_gpu(tmp_path: Path) -> None:
    # Guards the shipped catalog data, not just the mechanism. Since Parakeet
    # moved to ONNX Runtime nothing in the catalog is GPU-only any more; a
    # future entry that is should say so here deliberately.
    catalog = await BundledCatalog().fetch()
    assert [e.name for e in catalog.entries if e.requires.gpu] == []
    parakeet = next(e for e in catalog.entries if e.name == "parakeet")
    assert "600MB" in parakeet.requires.note  # the cost that *is* real
