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
import os
import sys
import threading
from typing import Any

import structlog
from pydantic import BaseModel

from prodeo.environment.cuda import CUDA12_RUNTIME, cuda_runtime_dirs, missing_cuda_libraries
from prodeo.plugins import PluginManifest
from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip

VERSION = "0.1.0"

_log = structlog.get_logger(__name__)


#: What CTranslate2 needs but does not ship, verified against the import table
#: of ``ctranslate2.dll`` (4.8.1): it links ``cublas64_12.dll`` - CUDA **12**,
#: not 13. The wheel bundles ``cudnn64_9.dll``, but that is only a dispatcher
#: which dlopens ``cudnn_ops64_9.dll`` and friends, and those are absent - the
#: classic "Could not locate cudnn_ops64_9.dll" failure. Discovery lives in
#: core so the environment view and this engine agree on what "CUDA is
#: installed" means; the dependency runs the right way round, since this
#: package already depends on ``prodeo``.
_REQUIRED_CUDA_DLLS = CUDA12_RUNTIME


def _add_cuda_dll_dirs() -> None:
    """Put the system CUDA runtime on the loader path on Windows.

    CTranslate2 does not register CUDA's DLL folders the way PyTorch does, so
    ``device="cuda"`` fails with "Library cublas64_12.dll is not found" until we
    do it. No-op off Windows (Linux resolves a system CUDA through ldconfig /
    LD_LIBRARY_PATH) and when nothing is installed - the caller falls back to
    CPU either way.
    """
    if sys.platform != "win32":
        return
    dirs = cuda_runtime_dirs()
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for bin_dir in dirs:
        with contextlib.suppress(OSError):
            os.add_dll_directory(bin_dir)
        # CTranslate2 loads cuBLAS/cuDNN via a path that ignores
        # add_dll_directory, so PATH is what actually makes them resolvable.
        if bin_dir not in path_parts:
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
            path_parts.append(bin_dir)
    if missing := missing_cuda_libraries(dirs, _REQUIRED_CUDA_DLLS):
        _log.warning("stt.cuda_runtime_incomplete", missing=missing, searched=dirs)


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
    """True when CTranslate2 (ships with faster-whisper) sees a CUDA device.

    This only proves the *driver* (``nvcuda.dll``) exists - it says nothing
    about cuBLAS/cuDNN, which is why ``auto`` also consults
    ``_cuda_runtime_usable``.
    """
    try:
        import ctranslate2

        return bool(ctranslate2.get_cuda_device_count() > 0)
    except Exception:
        return False


def _cuda_runtime_usable() -> bool:
    """False when ``device="cuda"`` is already known to be doomed.

    On Windows we can check for the required DLLs up front; a GPU whose
    runtime is incomplete would otherwise pass ``_cuda_available`` (the driver
    is there) and then blow up at the first encode. Off Windows we can't
    cheaply tell, so trust the driver check alone.
    """
    if sys.platform != "win32":
        return True
    return not missing_cuda_libraries(cuda_runtime_dirs(), _REQUIRED_CUDA_DLLS)


def _resolve_device(device: str, compute_type: str) -> tuple[str, str]:
    """Resolve the ``auto`` sentinels to a concrete (device, compute_type)."""
    resolved_device = device
    if device == "auto":
        resolved_device = "cuda" if _cuda_available() and _cuda_runtime_usable() else "cpu"
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

    def _probe(self, model: Any) -> None:
        """Transcribe a sliver of silence to force the CUDA runtime to load.

        CTranslate2 links cuBLAS/cuDNN *lazily at the first encode*, so a CUDA
        ``WhisperModel`` constructs fine on a machine with no usable runtime
        and only blows up mid-command ("Library cublas64_12.dll is not found"),
        past the constructor-level fallback. Probing here surfaces that while
        we can still fall back to CPU - and makes ``warmup`` genuinely warm.
        """
        import numpy

        silence = numpy.zeros(SAMPLE_RATE // 10, dtype=numpy.float32)
        segments, _info = model.transcribe(silence, language=self._config.language, beam_size=1)
        for _segment in segments:  # a lazy generator: encoding happens here
            pass

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
                    model = WhisperModel(
                        self._config.model,
                        device=device,
                        compute_type=compute_type,
                        download_root=root,
                    )
                    if device == "cuda":
                        self._probe(model)
                    self._model = model
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
        description=(
            "Default speech-to-text for Mjolnir. CPU-capable, and uses the GPU "
            "when a CUDA 12 runtime is installed machine-wide."
        ),
        publisher="Prodeo",
        homepage="https://github.com/bmays87/CommandCenter/tree/main/packages/prodeo-stt-fasterwhisper",
        license="Apache-2.0",
        categories=["voice", "gpu-optional"],
    )


__all__ = ["FasterWhisperConfig", "FasterWhisperStt", "create_stt", "manifest"]
