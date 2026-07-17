"""Mjölnir STT engine: faster-whisper (CTranslate2 Whisper).

Implements the ``SpeechToText`` Protocol (``prodeo_mjolnir.engines``). This
is the default CPU-capable engine; the heavier GPU alternative is
``prodeo-stt-parakeet``. Model loading and inference run in a worker thread
(``asyncio.to_thread``), never on the event loop. The model is loaded on
first use and cached: the first command after boot pays the load, everything
after is warm.
"""

import asyncio
import threading
from typing import Any

from pydantic import BaseModel

from prodeo.plugins import PluginManifest
from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip

VERSION = "0.1.0"


class FasterWhisperConfig(BaseModel):
    """Validated by the engine loader before construction."""

    model: str = "base.en"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str = "en"
    beam_size: int = 5
    #: Where model weights are cached (empty = the library default).
    download_root: str = ""


class FasterWhisperStt:
    """One Whisper transcription per captured utterance."""

    def __init__(self, config: FasterWhisperConfig) -> None:
        self._config = config
        self._model: Any = None  # WhisperModel; Any keeps the import lazy
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "faster-whisper"

    async def transcribe(self, clip: AudioClip) -> str:
        if clip.sample_rate != SAMPLE_RATE:
            raise ValueError(f"expected {SAMPLE_RATE} Hz audio, got {clip.sample_rate}")
        return await asyncio.to_thread(self._transcribe_sync, clip)

    def _transcribe_sync(self, clip: AudioClip) -> str:
        import numpy

        model = self._ensure_model()
        audio = numpy.frombuffer(clip.pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0
        segments, _info = model.transcribe(
            audio, language=self._config.language, beam_size=self._config.beam_size
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _ensure_model(self) -> Any:  # WhisperModel; Any keeps the import lazy
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel  # heavy: ctranslate2 et al.

                self._model = WhisperModel(
                    self._config.model,
                    device=self._config.device,
                    compute_type=self._config.compute_type,
                    download_root=self._config.download_root or None,
                )
            return self._model


def create_stt(config: FasterWhisperConfig) -> FasterWhisperStt:
    """Plugin factory: called by the engine loader with validated config."""
    return FasterWhisperStt(config)


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group): what this plugin is."""
    return PluginManifest(
        name="faster-whisper",
        kind="stt",
        version=VERSION,
        config_model=FasterWhisperConfig,
        factory=create_stt,
    )


__all__ = ["FasterWhisperConfig", "FasterWhisperStt", "create_stt", "manifest"]
