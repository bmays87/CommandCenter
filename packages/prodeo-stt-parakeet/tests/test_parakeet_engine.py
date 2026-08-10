"""Parakeet engine wrapper: PCM conversion, lazy loading, config plumbing.

``onnx_asr`` is stubbed via sys.modules, so no model weights are downloaded
and no ONNX session is created; the wrapper's own logic is what is under test.
"""

import os
import sys
import types
from pathlib import Path
from typing import Any

import numpy
import pytest

from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip
from prodeo_stt_parakeet import ParakeetConfig, ParakeetStt, manifest


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, int]] = []

    def recognize(self, waveform: Any, *, sample_rate: int = SAMPLE_RATE, **_: Any) -> str:
        self.calls.append((waveform, sample_rate))
        return "  status report  "


class FakeLoader:
    """Records how load_model was called."""

    def __init__(self) -> None:
        self.adapter = FakeAdapter()
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def load_model(self, *args: Any, **kwargs: Any) -> FakeAdapter:
        self.calls.append((args, kwargs))
        return self.adapter


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch) -> FakeLoader:
    fake = FakeLoader()
    module = types.ModuleType("onnx_asr")
    module.load_model = fake.load_model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnx_asr", module)
    return fake


def _clip(samples: int = SAMPLE_RATE) -> AudioClip:
    pcm = (numpy.ones(samples, dtype=numpy.int16) * 16384).tobytes()
    return AudioClip(pcm=pcm, sample_rate=SAMPLE_RATE)


@pytest.mark.asyncio
async def test_transcribe_passes_float32_pcm_and_strips(loader: FakeLoader) -> None:
    stt = ParakeetStt(ParakeetConfig())
    assert await stt.transcribe(_clip()) == "status report"

    # onnx-asr takes the waveform directly - no temporary WAV per utterance,
    # unlike the NeMo implementation this replaced.
    waveform, rate = loader.adapter.calls[0]
    assert waveform.dtype == numpy.float32
    assert rate == SAMPLE_RATE
    assert float(waveform[0]) == pytest.approx(0.5)  # 16384 / 32768


@pytest.mark.asyncio
async def test_model_is_loaded_once_and_cached(loader: FakeLoader) -> None:
    stt = ParakeetStt(ParakeetConfig())
    await stt.transcribe(_clip())
    await stt.transcribe(_clip())
    assert len(loader.calls) == 1
    assert len(loader.adapter.calls) == 2


@pytest.mark.asyncio
async def test_defaults_request_the_english_v2_model(loader: FakeLoader) -> None:
    await ParakeetStt(ParakeetConfig()).transcribe(_clip())
    (args, kwargs) = loader.calls[0]
    assert args[0] == "nemo-parakeet-tdt-0.6b-v2"
    assert args[1] is None  # no local path: fetch from Hugging Face
    assert kwargs == {"quantization": None, "providers": None}


@pytest.mark.asyncio
async def test_config_reaches_load_model(loader: FakeLoader) -> None:
    config = ParakeetConfig(
        model="nemo-parakeet-tdt-0.6b-v3",
        path="/models/parakeet",
        quantization="int8",
        providers=["CUDAExecutionProvider"],
    )
    await ParakeetStt(config).transcribe(_clip())

    (args, kwargs) = loader.calls[0]
    assert args == ("nemo-parakeet-tdt-0.6b-v3", "/models/parakeet")
    assert kwargs == {"quantization": "int8", "providers": ["CUDAExecutionProvider"]}


@pytest.mark.asyncio
async def test_download_root_sets_hf_home(
    loader: FakeLoader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HF_HOME", raising=False)
    await ParakeetStt(ParakeetConfig(download_root=str(tmp_path))).transcribe(_clip())
    assert os.environ["HF_HOME"] == str(tmp_path)


@pytest.mark.asyncio
async def test_an_existing_hf_home_is_not_overridden(
    loader: FakeLoader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HOME", "/already/chosen")
    await ParakeetStt(ParakeetConfig(download_root=str(tmp_path))).transcribe(_clip())
    assert os.environ["HF_HOME"] == "/already/chosen"


@pytest.mark.asyncio
async def test_wrong_sample_rate_is_rejected(loader: FakeLoader) -> None:
    stt = ParakeetStt(ParakeetConfig())
    with pytest.raises(ValueError, match="16000"):
        await stt.transcribe(AudioClip(pcm=b"\x00\x00", sample_rate=44_100))
    assert loader.calls == []  # rejected before any model load


@pytest.mark.asyncio
async def test_warmup_loads_without_transcribing(loader: FakeLoader) -> None:
    await ParakeetStt(ParakeetConfig()).warmup()
    assert len(loader.calls) == 1
    assert loader.adapter.calls == []


def test_manifest_shape() -> None:
    m = manifest()
    assert (m.name, m.kind) == ("parakeet", "stt")
    assert m.config_model is ParakeetConfig
    # No longer GPU-only: ONNX Runtime runs this on CPU too.
    assert "gpu-required" not in m.categories
