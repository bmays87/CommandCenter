"""Parakeet engine wrapper: WAV round-trip and lazy NeMo loading.

The package is not part of the workspace dev group (NeMo is multi-GB), so
this module skips entirely where it isn't installed; when it is, the NeMo
import is stubbed via sys.modules - no GPU or weights needed.
"""

import sys
import types
import wave
from pathlib import Path
from typing import Any, ClassVar

import pytest

prodeo_stt_parakeet = pytest.importorskip("prodeo_stt_parakeet")

from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip  # noqa: E402

ParakeetConfig = prodeo_stt_parakeet.ParakeetConfig
ParakeetStt = prodeo_stt_parakeet.ParakeetStt


class FakeAsrModel:
    transcribed: ClassVar[list[str]] = []

    @classmethod
    def from_pretrained(cls, model: str) -> "FakeAsrModel":
        cls.pretrained = model
        return cls()

    def transcribe(self, paths: list[str]) -> list[Any]:
        for path in paths:
            with wave.open(path, "rb") as f:
                assert f.getframerate() == SAMPLE_RATE
                assert f.getnchannels() == 1
            FakeAsrModel.transcribed.append(path)
        return [types.SimpleNamespace(text=" status report ")]


@pytest.fixture(autouse=True)
def fake_nemo(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsrModel.transcribed = []
    asr = types.ModuleType("nemo.collections.asr")
    asr.models = types.SimpleNamespace(ASRModel=FakeAsrModel)  # type: ignore[attr-defined]
    collections = types.ModuleType("nemo.collections")
    nemo = types.ModuleType("nemo")
    monkeypatch.setitem(sys.modules, "nemo", nemo)
    monkeypatch.setitem(sys.modules, "nemo.collections", collections)
    monkeypatch.setitem(sys.modules, "nemo.collections.asr", asr)


@pytest.mark.asyncio
async def test_transcribe_via_temp_wav() -> None:
    stt = ParakeetStt(ParakeetConfig())
    clip = AudioClip(pcm=b"\x00\x01" * SAMPLE_RATE, sample_rate=SAMPLE_RATE)
    assert await stt.transcribe(clip) == "status report"
    assert FakeAsrModel.pretrained == "nvidia/parakeet-tdt-0.6b-v2"
    assert not Path(FakeAsrModel.transcribed[0]).exists()  # temp dir cleaned up
