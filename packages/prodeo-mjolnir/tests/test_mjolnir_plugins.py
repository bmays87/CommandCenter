"""Engine loader: selection, config validation, wake word injection."""

from typing import Any

import pytest
from mjolnir_fakes import FakeStt, FakeTts, FakeWakeWord
from pydantic import BaseModel

from prodeo.plugins import PLUGIN_API_VERSION, PluginManifest
from prodeo_mjolnir.config import MjolnirSettings
from prodeo_mjolnir.errors import EngineNotFoundError, MjolnirError
from prodeo_mjolnir.plugins import load_engines


class FakeEntryPoint:
    def __init__(self, name: str, obj: Any) -> None:
        self.name = name
        self._obj = obj

    def load(self) -> Any:
        return self._obj


class WakeConfig(BaseModel):
    wake_word: str = ""
    sensitivity: float = 0.5


class RecordingWake(FakeWakeWord):
    def __init__(self, config: WakeConfig) -> None:
        super().__init__()
        self.config = config


class StubRephraser:
    @property
    def name(self) -> str:
        return "stub"

    async def summarize(self, instructions: str, content: str) -> str:
        return "prose"


def _entry_points(*manifests: PluginManifest) -> list[FakeEntryPoint]:
    return [FakeEntryPoint(m.name, m) for m in manifests]


def _manifests() -> list[PluginManifest]:
    return [
        PluginManifest(
            name="fake-wake",
            kind="wakeword",
            version="1.0",
            config_model=WakeConfig,
            factory=RecordingWake,
        ),
        PluginManifest(name="fake-stt", kind="stt", version="1.0", factory=lambda c: FakeStt([])),
        PluginManifest(name="fake-tts", kind="tts", version="1.0", factory=lambda c: FakeTts()),
        PluginManifest(
            name="stub", kind="summarizer", version="1.0", factory=lambda c: StubRephraser()
        ),
    ]


def _settings(**overrides: object) -> MjolnirSettings:
    defaults: dict[str, object] = {
        "wakeword_plugin": "fake-wake",
        "stt_plugin": "fake-stt",
        "tts_plugin": "fake-tts",
    }
    defaults.update(overrides)
    return MjolnirSettings(**defaults)  # type: ignore[arg-type]  # test-only kwargs passthrough


def test_loads_selected_engines_and_injects_wake_word() -> None:
    settings = _settings(wake_word="hey hammer")
    engines = load_engines(settings, entry_points_fn=lambda: _entry_points(*_manifests()))

    assert isinstance(engines.wakeword, RecordingWake)
    assert engines.wakeword.config.wake_word == "hey hammer"
    assert engines.stt.name == "fake-stt"
    assert engines.tts.name == "fake-tts"
    assert list(engines.rephrasers) == ["stub"]  # summarizer kind rides along


def test_per_engine_config_wins_over_injected_wake_word() -> None:
    settings = _settings(
        wake_word="hey hammer",
        engines={"fake-wake": {"wake_word": "custom", "sensitivity": 0.9}},
    )
    engines = load_engines(settings, entry_points_fn=lambda: _entry_points(*_manifests()))
    assert isinstance(engines.wakeword, RecordingWake)
    assert engines.wakeword.config.wake_word == "custom"
    assert engines.wakeword.config.sensitivity == 0.9


def test_missing_engine_is_a_clear_error() -> None:
    settings = _settings(stt_plugin="whisper-x")
    with pytest.raises(EngineNotFoundError, match=r"whisper-x.*fake-stt"):
        load_engines(settings, entry_points_fn=lambda: _entry_points(*_manifests()))


def test_api_version_mismatch_is_refused() -> None:
    stale = PluginManifest(
        name="fake-wake",
        kind="wakeword",
        version="1.0",
        plugin_api_version=PLUGIN_API_VERSION + 1,
        factory=lambda c: FakeWakeWord(),
    )
    manifests = [stale, *_manifests()[1:]]
    with pytest.raises(MjolnirError, match="version mismatch"):
        load_engines(_settings(), entry_points_fn=lambda: _entry_points(*manifests))


def test_wrong_product_is_refused() -> None:
    manifests = _manifests()
    manifests[1] = PluginManifest(
        name="fake-stt", kind="stt", version="1.0", factory=lambda c: object()
    )
    with pytest.raises(MjolnirError, match="did not produce"):
        load_engines(_settings(), entry_points_fn=lambda: _entry_points(*manifests))


def test_unloadable_entry_point_is_skipped() -> None:
    def explodes() -> None:
        raise ImportError("missing heavy dependency")

    eps = [FakeEntryPoint("boom", explodes), *_entry_points(*_manifests())]
    engines = load_engines(_settings(), entry_points_fn=lambda: eps)
    assert engines.stt.name == "fake-stt"
