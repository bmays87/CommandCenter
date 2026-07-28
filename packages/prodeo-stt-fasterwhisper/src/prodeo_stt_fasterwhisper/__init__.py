"""Mjölnir STT engine: faster-whisper (CTranslate2 Whisper).

Implements the ``SpeechToText`` Protocol (``prodeo_mjolnir.engines``). This
is the default CPU-capable engine; the heavier GPU alternative is
``prodeo-stt-parakeet``. Model loading and inference run in a worker thread
(``asyncio.to_thread``), never on the event loop. The model is loaded on
first use and cached: the first command after boot pays the load, everything
after is warm.
"""

import asyncio
import contextlib
import glob
import os
import sys
import threading
from typing import Any

import structlog
from pydantic import BaseModel

from prodeo.plugins import PluginManifest
from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip

VERSION = "0.1.0"

_log = structlog.get_logger(__name__)


def _add_cuda_dll_dirs() -> None:
    """Register the pip CUDA wheels' DLL folders on Windows.

    ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12`` ship ``cublas64_12.dll`` and
    ``cudnn64_9.dll`` under ``site-packages/nvidia/*/bin``, but CTranslate2 does
    not add those to the loader path the way PyTorch does - so ``device="cuda"``
    fails with "Library cublas64_12.dll is not found" until we register them.
    No-op off Windows and when the wheels aren't installed.
    """
    if sys.platform != "win32":
        return
    base = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for bin_dir in glob.glob(os.path.join(base, "*", "bin")):
        with contextlib.suppress(OSError):
            os.add_dll_directory(bin_dir)
        # CTranslate2 loads cuBLAS/cuDNN via a path that ignores
        # add_dll_directory, so PATH is what actually makes them resolvable.
        if bin_dir not in path_parts:
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
            path_parts.append(bin_dir)


class FasterWhisperConfig(BaseModel):
    """Validated by the engine loader before construction."""

    model: str = "base.en"
    #: ``auto`` (default) uses CUDA when a GPU is present, else CPU. Pin to
    #: ``cuda``/``cpu`` to override the detection.
    device: str = "auto"
    #: ``auto`` (default) picks ``float16`` on CUDA and ``int8`` on CPU. Pin to
    #: any CTranslate2 compute type to override.
    compute_type: str = "auto"
    language: str = "en"
    beam_size: int = 5
    #: Where model weights are cached (empty = the library default).
    download_root: str = ""


def _cuda_available() -> bool:
    """True when CTranslate2 (ships with faster-whisper) sees a CUDA device."""
    try:
        import ctranslate2

        return bool(ctranslate2.get_cuda_device_count() > 0)
    except Exception:
        return False


def _resolve_device(device: str, compute_type: str) -> tuple[str, str]:
    """Resolve the ``auto`` sentinels to a concrete (device, compute_type)."""
    resolved_device = device
    if device == "auto":
        resolved_device = "cuda" if _cuda_available() else "cpu"
    resolved_compute = compute_type
    if compute_type == "auto":
        resolved_compute = "float16" if resolved_device == "cuda" else "int8"
    return resolved_device, resolved_compute


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

    async def warmup(self) -> None:
        """Load (and, on first ever run, download) the model off the critical
        path so the first real command is warm - the ``Warmable`` capability."""
        await asyncio.to_thread(self._ensure_model)

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
                _add_cuda_dll_dirs()  # so device="cuda" can load its runtime on Windows
                from faster_whisper import WhisperModel  # heavy: ctranslate2 et al.

                device, compute_type = _resolve_device(
                    self._config.device, self._config.compute_type
                )
                root = self._config.download_root or None
                try:
                    self._model = WhisperModel(
                        self._config.model,
                        device=device,
                        compute_type=compute_type,
                        download_root=root,
                    )
                except Exception:
                    if device != "cuda":
                        raise
                    # CUDA was detected but the runtime failed to load - fall
                    # back to CPU rather than leaving the client mute.
                    _log.warning(
                        "stt.cuda_unavailable_fallback", model=self._config.model, exc_info=True
                    )
                    self._model = WhisperModel(
                        self._config.model,
                        device="cpu",
                        compute_type="int8",
                        download_root=root,
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
