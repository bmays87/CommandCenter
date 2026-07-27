"""Piper engine wrapper: chunk assembly, speaker passthrough, lazy caching,
GPU auto-detection.

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
    cuda_flags: ClassVar[list[bool]] = []
    #: When True, a load requesting CUDA raises (models a missing GPU runtime).
    fail_on_cuda: ClassVar[bool] = False

    def __init__(self) -> None:
        self.config = types.SimpleNamespace(sample_rate=22_050)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @classmethod
    def load(cls, path: str, use_cuda: bool = False) -> "FakeVoice":
        cls.loads.append(path)
        cls.cuda_flags.append(use_cuda)
        if cls.fail_on_cuda and use_cuda:
            raise RuntimeError("CUDA execution provider unavailable")
        return cls._instance

    def synthesize(self, text: str, **kwargs: Any) -> list[Chunk]:
        self.calls.append((text, kwargs))
        return [Chunk(b"\x01\x02"), Chunk(b"\x03\x04")]

    _instance: "FakeVoice"


@pytest.fixture(autouse=True)
def fake_piper(monkeypatch: pytest.MonkeyPatch) -> FakeVoice:
    FakeVoice.loads = []
    FakeVoice.cuda_flags = []
    FakeVoice.fail_on_cuda = False
    FakeVoice._instance = FakeVoice()
    module = types.ModuleType("piper")
    module.PiperVoice = FakeVoice  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "piper", module)
    # Deterministic default: no GPU unless a test says otherwise.
    monkeypatch.setattr("prodeo_tts_piper._cuda_provider_available", lambda: False)
    return FakeVoice._instance


@pytest.mark.asyncio
async def test_synthesize_assembles_chunks_and_caches(fake_piper: FakeVoice) -> None:
    tts = PiperTts(PiperTtsConfig(voice_path="/opt/voices/en_GB-alan-medium.onnx"))
    clip = await tts.synthesize("Approved, sir.")

    assert clip.pcm == b"\x01\x02\x03\x04"
    assert clip.sample_rate == 22_050
    assert FakeVoice.loads == ["/opt/voices/en_GB-alan-medium.onnx"]
    assert FakeVoice.cuda_flags == [False]  # auto-detect found no GPU
    assert fake_piper.calls[0] == ("Approved, sir.", {})

    await tts.synthesize("Again.")
    assert FakeVoice.loads == ["/opt/voices/en_GB-alan-medium.onnx"]  # loaded once


@pytest.mark.asyncio
async def test_speaker_id_passthrough(fake_piper: FakeVoice) -> None:
    tts = PiperTts(PiperTtsConfig(voice_path="/v.onnx", speaker_id=3))
    await tts.synthesize("Hello.")
    assert fake_piper.calls[0][1] == {"speaker_id": 3}


@pytest.mark.asyncio
async def test_cuda_used_when_provider_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prodeo_tts_piper._cuda_provider_available", lambda: True)
    tts = PiperTts(PiperTtsConfig(voice_path="/v.onnx"))
    await tts.synthesize("Hello.")
    assert FakeVoice.cuda_flags == [True]  # auto-detect enabled the GPU


@pytest.mark.asyncio
async def test_cuda_load_failure_falls_back_to_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prodeo_tts_piper._cuda_provider_available", lambda: True)
    FakeVoice.fail_on_cuda = True
    tts = PiperTts(PiperTtsConfig(voice_path="/v.onnx"))
    clip = await tts.synthesize("Hello.")
    assert clip.pcm == b"\x01\x02\x03\x04"  # still spoke
    assert FakeVoice.cuda_flags == [True, False]  # tried CUDA, retried on CPU


@pytest.mark.asyncio
async def test_use_cuda_explicit_false_skips_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provider is available, but an explicit False must win over auto-detect.
    monkeypatch.setattr("prodeo_tts_piper._cuda_provider_available", lambda: True)
    tts = PiperTts(PiperTtsConfig(voice_path="/v.onnx", use_cuda=False))
    await tts.synthesize("Hello.")
    assert FakeVoice.cuda_flags == [False]


def test_manifest_shape_and_required_voice() -> None:
    m = manifest()
    assert (m.name, m.kind) == ("piper", "tts")
    assert m.config_model is PiperTtsConfig
    with pytest.raises(ValueError):  # voice_path is required, by design
        PiperTtsConfig()  # type: ignore[call-arg]
