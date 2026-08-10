"""Mjölnir STT engine: NVIDIA Parakeet via ONNX Runtime.

Implements the ``SpeechToText`` Protocol (``prodeo_mjolnir.engines``). Same
model as NVIDIA's NeMo distribution, run through ONNX Runtime instead: the
dependency chain is numpy + onnxruntime rather than PyTorch, so this installs
anywhere faster-whisper does and runs on CPU as well as GPU.

The ``onnx_asr`` import stays lazy and inference runs in a worker thread, as
with every engine - model loading is slow and blocking, and neither belongs on
the event loop.
"""

import asyncio
import os
import threading
from typing import Any

import structlog
from pydantic import BaseModel, Field

from prodeo.plugins import PluginManifest
from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip

VERSION = "0.2.0"

_log = structlog.get_logger(__name__)


class ParakeetConfig(BaseModel):
    """Validated by the engine loader before construction."""

    #: onnx-asr model id. ``-v2`` is English; ``-v3`` is multilingual. Other
    #: accepted ids include ``nemo-parakeet-ctc-0.6b`` and
    #: ``nemo-parakeet-rnnt-0.6b``.
    model: str = "nemo-parakeet-tdt-0.6b-v2"
    #: Directory of already-downloaded model files; empty = fetch from
    #: Hugging Face on first use.
    path: str = ""
    #: e.g. ``int8`` for a smaller, faster model at some accuracy cost.
    quantization: str = ""
    #: Where downloaded models are cached. Sets ``HF_HOME``, which is the only
    #: lever onnx-asr's Hugging Face download exposes; an existing ``HF_HOME``
    #: always wins. Empty = the Hugging Face default (your home directory).
    download_root: str = ""
    #: ONNX Runtime execution providers, most preferred first - e.g.
    #: ``["CUDAExecutionProvider"]`` or ``["DmlExecutionProvider"]`` on
    #: Windows. Requires the matching onnxruntime build (``onnxruntime-gpu`` /
    #: ``onnxruntime-directml``); empty = the runtime's own default order,
    #: which is CPU for the stock package.
    providers: list[str] = Field(default_factory=list)


class ParakeetStt:
    """One Parakeet transcription per captured utterance."""

    def __init__(self, config: ParakeetConfig) -> None:
        self._config = config
        self._model: Any = None  # onnx_asr adapter; Any keeps the import lazy
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "parakeet"

    async def transcribe(self, clip: AudioClip) -> str:
        if clip.sample_rate != SAMPLE_RATE:
            raise ValueError(f"expected {SAMPLE_RATE} Hz audio, got {clip.sample_rate}")
        return await asyncio.to_thread(self._transcribe_sync, clip)

    async def warmup(self) -> None:
        """Load (and on first ever run download) the model off the critical
        path so the first real command is warm - the ``Warmable`` capability."""
        await asyncio.to_thread(self._ensure_model)

    def _transcribe_sync(self, clip: AudioClip) -> str:
        import numpy

        model = self._ensure_model()
        # onnx-asr takes float32 PCM directly, so unlike the NeMo path there is
        # no temporary WAV round-trip per utterance.
        audio = numpy.frombuffer(clip.pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0
        result = model.recognize(audio, sample_rate=clip.sample_rate)
        return str(result).strip()

    def _ensure_model(self) -> Any:  # onnx_asr adapter; Any keeps the import lazy
        with self._lock:
            if self._model is None:
                if self._config.download_root:
                    # Must be set before huggingface_hub first resolves its
                    # cache; setdefault so an explicit HF_HOME still wins.
                    os.environ.setdefault("HF_HOME", self._config.download_root)
                import onnx_asr  # heavy: onnxruntime session + model weights

                _log.info("parakeet.loading", model=self._config.model)
                self._model = onnx_asr.load_model(
                    self._config.model,
                    self._config.path or None,
                    quantization=self._config.quantization or None,
                    providers=self._config.providers or None,
                )
            return self._model


def create_stt(config: ParakeetConfig) -> ParakeetStt:
    """Plugin factory: called by the engine loader with validated config."""
    return ParakeetStt(config)


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group): what this plugin is."""
    return PluginManifest(
        name="parakeet",
        kind="stt",
        version=VERSION,
        config_model=ParakeetConfig,
        factory=create_stt,
        description=(
            "NVIDIA Parakeet speech-to-text via ONNX Runtime. Higher accuracy "
            "than the default; runs on CPU, faster on GPU."
        ),
        publisher="Prodeo",
        homepage="https://github.com/bmays87/CommandCenter/tree/main/packages/prodeo-stt-parakeet",
        license="Apache-2.0",
        categories=["voice", "large-download"],
    )


__all__ = ["ParakeetConfig", "ParakeetStt", "create_stt", "manifest"]
