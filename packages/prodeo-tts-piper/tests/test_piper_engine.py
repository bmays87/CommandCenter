"""Piper engine wrapper: chunk assembly, speaker passthrough, lazy caching.

The real piper library is stubbed via sys.modules so no voice files are
needed; the wrapper's own logic is what's under test.
"""

import sys
import types
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from prodeo_tts_piper import PiperTts, PiperTtsConfig, manifest


@dataclass
class Chunk:
    audio_int16_bytes: bytes
    sample_rate: int = 22_050


class FakeVoice:
    loads: ClassVar[list[str]] = []

    def __init__(self) -> None:
        self.config = types.SimpleNamespace(sample_rate=22_050)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @classmethod
    def load(cls, path: str) -> "FakeVoice":
        cls.loads.append(path)
        return cls._instance

    def synthesize(self, text: str, **kwargs: Any) -> list[Chunk]:
        self.calls.append((text, kwargs))
        return [Chunk(b"\x01\x02"), Chunk(b"\x03\x04")]

    _instance: "FakeVoice"


@pytest.fixture(autouse=True)
def fake_piper(monkeypatch: pytest.MonkeyPatch) -> FakeVoice:
    FakeVoice.loads = []
    FakeVoice._instance = FakeVoice()
    module = types.ModuleType("piper")
    module.PiperVoice = FakeVoice  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "piper", module)
    return FakeVoice._instance


@pytest.mark.asyncio
async def test_synthesize_assembles_chunks_and_caches(fake_piper: FakeVoice) -> None:
    tts = PiperTts(PiperTtsConfig(voice_path="/opt/voices/en_GB-alan-medium.onnx"))
    clip = await tts.synthesize("Approved, sir.")

    assert clip.pcm == b"\x01\x02\x03\x04"
    assert clip.sample_rate == 22_050
    assert FakeVoice.loads == ["/opt/voices/en_GB-alan-medium.onnx"]
    assert fake_piper.calls[0] == ("Approved, sir.", {})

    await tts.synthesize("Again.")
    assert FakeVoice.loads == ["/opt/voices/en_GB-alan-medium.onnx"]  # loaded once


@pytest.mark.asyncio
async def test_speaker_id_passthrough(fake_piper: FakeVoice) -> None:
    tts = PiperTts(PiperTtsConfig(voice_path="/v.onnx", speaker_id=3))
    await tts.synthesize("Hello.")
    assert fake_piper.calls[0][1] == {"speaker_id": 3}


def test_manifest_shape_and_required_voice() -> None:
    m = manifest()
    assert (m.name, m.kind) == ("piper", "tts")
    assert m.config_model is PiperTtsConfig
    with pytest.raises(ValueError):  # voice_path is required, by design
        PiperTtsConfig()  # type: ignore[call-arg]
