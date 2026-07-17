"""faster-whisper engine wrapper: audio conversion, joining, lazy caching.

The real faster_whisper library is stubbed via sys.modules so no model
weights are needed; the wrapper's own logic is what's under test.
"""

import sys
import types
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy
import pytest

from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip
from prodeo_stt_fasterwhisper import FasterWhisperConfig, FasterWhisperStt, manifest


@dataclass
class Segment:
    text: str


class FakeWhisperModel:
    instances: ClassVar[list["FakeWhisperModel"]] = []

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.model = model
        self.kwargs = kwargs
        self.audio: list[Any] = []
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[list[Segment], object]:
        self.audio.append(audio)
        return [Segment(" What happened "), Segment("overnight? ")], object()


@pytest.fixture(autouse=True)
def fake_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWhisperModel.instances = []
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)


def _clip(samples: int = SAMPLE_RATE) -> AudioClip:
    pcm = (numpy.ones(samples, dtype=numpy.int16) * 16384).tobytes()
    return AudioClip(pcm=pcm, sample_rate=SAMPLE_RATE)


@pytest.mark.asyncio
async def test_transcribe_converts_joins_and_caches() -> None:
    stt = FasterWhisperStt(FasterWhisperConfig(model="base.en", compute_type="int8"))
    text = await stt.transcribe(_clip())
    assert text == "What happened overnight?"

    model = FakeWhisperModel.instances[0]
    assert model.model == "base.en"
    assert model.kwargs["compute_type"] == "int8"
    assert model.kwargs["download_root"] is None
    audio = model.audio[0]
    assert audio.dtype == numpy.float32
    assert float(audio[0]) == pytest.approx(0.5)  # 16384 / 32768

    await stt.transcribe(_clip())
    assert len(FakeWhisperModel.instances) == 1  # model loaded once, cached


@pytest.mark.asyncio
async def test_wrong_sample_rate_is_rejected() -> None:
    stt = FasterWhisperStt(FasterWhisperConfig())
    with pytest.raises(ValueError, match="16000"):
        await stt.transcribe(AudioClip(pcm=b"\x00\x00", sample_rate=44_100))
    assert FakeWhisperModel.instances == []  # rejected before any model load


def test_manifest_shape() -> None:
    m = manifest()
    assert (m.name, m.kind) == ("faster-whisper", "stt")
    assert m.config_model is FasterWhisperConfig
