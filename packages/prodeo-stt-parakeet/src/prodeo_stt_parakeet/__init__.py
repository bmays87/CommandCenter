"""Mjölnir STT engine: NVIDIA Parakeet via NeMo (GPU).

Implements the ``SpeechToText`` Protocol (``prodeo_mjolnir.engines``). The
higher-accuracy, heavier alternative to ``prodeo-stt-fasterwhisper`` - its
NeMo dependency chain is multi-GB and CUDA-bound, which is exactly why STT
engines are separate plugin packages (voice-pipeline.md). All NeMo imports
are lazy; inference runs in a worker thread via a temporary WAV file (the
most stable NeMo transcription surface across versions).
"""

import asyncio
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from prodeo.plugins import PluginManifest
from prodeo_mjolnir.engines import AudioClip

VERSION = "0.1.0"


class ParakeetConfig(BaseModel):
    """Validated by the engine loader before construction."""

    model: str = "nvidia/parakeet-tdt-0.6b-v2"


class ParakeetStt:
    """One Parakeet transcription per captured utterance."""

    def __init__(self, config: ParakeetConfig) -> None:
        self._config = config
        self._model: Any = None  # nemo ASRModel; Any keeps the import lazy
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "parakeet"

    async def transcribe(self, clip: AudioClip) -> str:
        return await asyncio.to_thread(self._transcribe_sync, clip)

    def _transcribe_sync(self, clip: AudioClip) -> str:
        model = self._ensure_model()
        with tempfile.TemporaryDirectory(prefix="parakeet-") as tmp:
            path = Path(tmp) / "utterance.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(clip.sample_rate)
                wav.writeframes(clip.pcm)
            outputs = model.transcribe([str(path)])
        if not outputs:
            return ""
        first = outputs[0]
        return str(getattr(first, "text", first)).strip()

    def _ensure_model(self) -> Any:  # nemo ASRModel; Any keeps the import lazy
        with self._lock:
            if self._model is None:
                import nemo.collections.asr as nemo_asr  # brutal: multi-GB, CUDA

                self._model = nemo_asr.models.ASRModel.from_pretrained(self._config.model)
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
    )


__all__ = ["ParakeetConfig", "ParakeetStt", "create_stt", "manifest"]
