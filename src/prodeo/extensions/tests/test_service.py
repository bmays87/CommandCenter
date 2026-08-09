"""ExtensionService: inventory presentation and config precedence."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from prodeo.errors import UnknownExtensionError
from prodeo.extensions import ExtensionService, JsonFileConfigStore
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


def test_inventory_is_read_lazily(tmp_path: Path) -> None:
    # The composition root wires the service before the host has loaded, so a
    # snapshot taken at construction time would always be empty.
    inventory: list[ExtensionInfo] = []
    svc = _service(tmp_path, inventory)
    assert svc.list() == []
    inventory.append(ExtensionInfo("ollama", "loaded", manifest=_manifest()))
    assert [s.name for s in svc.list()] == ["ollama"]
