"""Asset provisioning: presence checks, download, and config wiring.

The download subprocess is real - a short python -c stands in for the actual
downloader - so the command path and produces-verification are genuinely
exercised.
"""

import json
import sys
from pathlib import Path

import pytest

from prodeo.errors import UnknownExtensionError
from prodeo.extensions import (
    AssetProvisioner,
    Catalog,
    CatalogEntry,
    ExtensionAsset,
    JsonFileConfigStore,
)


class FakeCatalog:
    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries = entries

    async def fetch(self) -> Catalog:
        return Catalog(source="fake", entries=self._entries)


def _writer(*relative: str) -> list[str]:
    """A command that creates the given files under {models_dir}."""
    code = (
        "import pathlib,sys\n"
        "for p in sys.argv[1:]:\n"
        "    q = pathlib.Path(p); q.parent.mkdir(parents=True, exist_ok=True)\n"
        "    q.write_text('x')\n"
    )
    return ["{python}", "-c", code, *relative]


def _asset(**overrides: object) -> ExtensionAsset:
    kwargs: dict[str, object] = {
        "id": "voice",
        "label": "Test voice",
        "approx_mb": 1,
        "command": _writer("{models_dir}/voices/v.onnx", "{models_dir}/voices/v.onnx.json"),
        "into": "{models_dir}/voices",
        "produces": ["{models_dir}/voices/v.onnx", "{models_dir}/voices/v.onnx.json"],
        "config_app": "mjolnir",
        "config_pointer": "engines.piper.voice_path",
    }
    kwargs.update(overrides)
    return ExtensionAsset.model_validate(kwargs)


def _provisioner(tmp_path: Path, asset: ExtensionAsset | None = None) -> AssetProvisioner:
    entry = CatalogEntry(name="piper", package="prodeo-tts-piper", assets=[asset or _asset()])
    return AssetProvisioner(
        catalog=FakeCatalog([entry]),
        store=JsonFileConfigStore(tmp_path / "extensions.json"),
        models_dir_fn=lambda: str(tmp_path / "models"),
        python=sys.executable,
    )


@pytest.mark.asyncio
async def test_missing_asset_is_reported_with_its_paths(tmp_path: Path) -> None:
    (status,) = await _provisioner(tmp_path).list_assets("piper")
    assert status.present is False
    assert len(status.missing) == 2
    assert status.approx_mb == 1
    assert str(tmp_path / "models") in status.produces[0]


@pytest.mark.asyncio
async def test_download_creates_files_and_wires_config(tmp_path: Path) -> None:
    prov = _provisioner(tmp_path)
    result = await prov.download("piper", "voice")

    assert result.ok is True
    assert Path(result.path).exists()
    assert result.configured == "mjolnir:engines.piper.voice_path"

    # The dotted pointer reached into the nested engines map without core
    # knowing what any of those names mean.
    saved = json.loads((tmp_path / "extensions.json").read_text(encoding="utf-8"))
    assert saved["config"]["mjolnir"]["engines"]["piper"]["voice_path"] == result.path

    assert (await prov.list_assets("piper"))[0].present is True


@pytest.mark.asyncio
async def test_a_download_missing_its_sibling_file_is_a_failure(tmp_path: Path) -> None:
    # The silent-mute bug in one test: Piper loads <voice>.onnx AND its
    # .onnx.json, and a voice without the sibling starts Mjolnir cleanly and
    # leaves it mute. Exit code 0 is not enough; the declared outputs decide.
    half = _asset(command=_writer("{models_dir}/voices/v.onnx"))
    result = await _provisioner(tmp_path, half).download("piper", "voice")

    assert result.ok is False
    assert "v.onnx.json" in result.error
    assert "missing" in result.error


@pytest.mark.asyncio
async def test_an_already_present_asset_still_wires_config(tmp_path: Path) -> None:
    # Someone who downloaded the voice by hand should get it configured by
    # re-running setup, not told there is nothing to do.
    voices = tmp_path / "models" / "voices"
    voices.mkdir(parents=True)
    (voices / "v.onnx").write_text("x", encoding="utf-8")
    (voices / "v.onnx.json").write_text("{}", encoding="utf-8")

    result = await _provisioner(tmp_path).download("piper", "voice")

    assert result.ok is True and result.output == "already present"
    assert result.configured == "mjolnir:engines.piper.voice_path"


@pytest.mark.asyncio
async def test_a_failing_downloader_is_contained(tmp_path: Path) -> None:
    broken = _asset(command=["{python}", "-c", "import sys; sys.exit(3)"])
    result = await _provisioner(tmp_path, broken).download("piper", "voice")
    assert result.ok is False and "exited with 3" in result.error


@pytest.mark.asyncio
async def test_unknown_asset_and_extension_are_refused(tmp_path: Path) -> None:
    prov = _provisioner(tmp_path)
    with pytest.raises(UnknownExtensionError, match="no asset"):
        await prov.download("piper", "nope")
    with pytest.raises(UnknownExtensionError, match="catalog"):
        await prov.download("not-a-thing", "voice")


@pytest.mark.asyncio
async def test_existing_config_is_preserved_around_the_pointer(tmp_path: Path) -> None:
    store = JsonFileConfigStore(tmp_path / "extensions.json")
    await store.put("mjolnir", {"wake_word": "mjölnir", "engines": {"faster-whisper": {"a": 1}}})
    prov = AssetProvisioner(
        catalog=FakeCatalog(
            [CatalogEntry(name="piper", package="prodeo-tts-piper", assets=[_asset()])]
        ),
        store=store,
        models_dir_fn=lambda: str(tmp_path / "models"),
        python=sys.executable,
    )

    await prov.download("piper", "voice")

    saved = await store.get("mjolnir") or {}
    assert saved["wake_word"] == "mjölnir"  # untouched
    assert saved["engines"]["faster-whisper"] == {"a": 1}  # sibling engine untouched
    assert saved["engines"]["piper"]["voice_path"]


@pytest.mark.asyncio
async def test_bundled_catalog_declares_the_piper_voice(tmp_path: Path) -> None:
    from prodeo.extensions import BundledCatalog

    catalog = await BundledCatalog().fetch()
    piper = next(e for e in catalog.entries if e.name == "piper")
    (asset,) = piper.assets
    # Both files, or the sibling check that prevents a silently mute client
    # would not exist.
    assert len(asset.produces) == 2
    assert asset.produces[1].endswith(".onnx.json")
    assert asset.config_pointer == "engines.piper.voice_path"


# --- app readiness (ADR-0017) ------------------------------------------------


def _readiness_provisioner(tmp_path: Path) -> tuple[AssetProvisioner, JsonFileConfigStore]:
    store = JsonFileConfigStore(tmp_path / "extensions.json")
    entry = CatalogEntry(name="piper", package="prodeo-tts-piper", assets=[_asset()])
    prov = AssetProvisioner(
        catalog=FakeCatalog([entry]),
        store=store,
        models_dir_fn=lambda: str(tmp_path / "models"),
        python=sys.executable,
    )
    return prov, store


@pytest.mark.asyncio
async def test_app_with_no_saved_config_has_a_gap(tmp_path: Path) -> None:
    prov, _ = _readiness_provisioner(tmp_path)

    (gap,) = await prov.unmet_for_app("mjolnir")

    assert "Test voice" in gap.description
    assert gap.config_pointer == "engines.piper.voice_path"


@pytest.mark.asyncio
async def test_configured_and_present_asset_is_no_gap(tmp_path: Path) -> None:
    prov, store = _readiness_provisioner(tmp_path)
    voice = tmp_path / "v.onnx"
    voice.write_text("x")
    await store.put("mjolnir", {"engines": {"piper": {"voice_path": str(voice)}}})

    assert await prov.unmet_for_app("mjolnir") == []


@pytest.mark.asyncio
async def test_configured_but_deleted_file_is_reported_as_missing(tmp_path: Path) -> None:
    # The sharp edge: config present, file gone. The app might start cleanly
    # and misbehave silently, which is worse than not starting.
    prov, store = _readiness_provisioner(tmp_path)
    await store.put("mjolnir", {"engines": {"piper": {"voice_path": str(tmp_path / "gone.onnx")}}})

    (gap,) = await prov.unmet_for_app("mjolnir")

    assert "missing from disk" in gap.description


@pytest.mark.asyncio
async def test_apps_without_declared_assets_have_no_gaps(tmp_path: Path) -> None:
    prov, _ = _readiness_provisioner(tmp_path)

    assert await prov.unmet_for_app("some-other-app") == []
