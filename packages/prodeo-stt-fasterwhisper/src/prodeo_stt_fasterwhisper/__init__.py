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
from collections.abc import Mapping, Sequence
from typing import Any

import structlog
from pydantic import BaseModel

from prodeo.plugins import PluginManifest
from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip

VERSION = "0.1.0"

_log = structlog.get_logger(__name__)


#: The CUDA libraries CTranslate2 needs but does *not* ship, verified against
#: the import table of ``ctranslate2.dll`` (4.8.1): it links ``cublas64_12.dll``
#: - CUDA **12**, not 13 - plus the always-present driver ``nvcuda.dll``.
#: The wheel does bundle ``cudnn64_9.dll``, but that is only a dispatcher which
#: in turn dlopens ``cudnn_ops64_9.dll`` / ``cudnn_graph64_9.dll`` / ... , and
#: those are absent - the classic "Could not locate cudnn_ops64_9.dll" failure.
#: So we probe for one representative backend rather than the dispatcher.
#: Bump these - and the ``v1[23].*`` globs below - when CTranslate2 moves to
#: CUDA 13; re-check with a string dump of ``ctranslate2.dll`` rather than
#: guessing.
_REQUIRED_CUDA_DLLS = ("cublas64_12.dll", "cudnn_ops64_9.dll")


def _cuda_runtime_dirs(
    env: Mapping[str, str] | None = None, prefix: str | None = None
) -> list[str]:
    """Directories that may hold the CUDA runtime, best candidate first.

    CUDA is a *machine-wide* prerequisite, not a Python dependency: pip's
    ``nvidia-*`` wheels would be pruned by the next ``uv sync`` (it removes
    anything the lock doesn't call for), so we look for a system install first.
    cuDNN ships as its own installer under a separate tree, hence the two
    families of roots.

    ``env``/``prefix`` are injectable so tests don't touch the real filesystem.
    """
    environ = os.environ if env is None else env
    py_prefix = sys.prefix if prefix is None else prefix
    program_files = environ.get("ProgramFiles", r"C:\Program Files")
    candidates: list[str] = []

    # CUDA_PATH itself is *not* consulted: it points at whichever toolkit was
    # installed last, which is routinely an older major (e.g. v11.0) that has
    # no cublas64_12.dll. Only explicitly-versioned roots are trusted.
    for name, value in environ.items():
        if name.startswith(("CUDA_PATH_V12_", "CUDA_PATH_V13_")):
            candidates.append(os.path.join(value, "bin"))
    candidates += glob.glob(
        os.path.join(program_files, "NVIDIA GPU Computing Toolkit", "CUDA", "v1[23].*", "bin")
    )

    # cuDNN 9 for Windows installs to its own tree, with the DLLs one level
    # deeper under a CUDA-major folder (bin/12.9); older layouts use bin/.
    cudnn_root = os.path.join(program_files, "NVIDIA", "CUDNN")
    candidates += glob.glob(os.path.join(cudnn_root, "v9.*", "bin", "1[23].*"))
    candidates += glob.glob(os.path.join(cudnn_root, "v9.*", "bin"))
    if cudnn_path := environ.get("CUDNN_PATH"):
        candidates.append(os.path.join(cudnn_path, "bin"))

    # Last resort: the pip wheels, if someone installed them into this venv.
    # Still supported, but it is the fragile path - see the docstring.
    candidates += glob.glob(os.path.join(py_prefix, "Lib", "site-packages", "nvidia", "*", "bin"))

    seen: set[str] = set()
    found: list[str] = []
    for directory in candidates:
        key = os.path.normcase(directory)
        if key not in seen and os.path.isdir(directory):
            seen.add(key)
            found.append(directory)
    return found


def _missing_cuda_libs(dirs: Sequence[str]) -> list[str]:
    """Which of ``_REQUIRED_CUDA_DLLS`` are in none of ``dirs``."""
    return [
        dll
        for dll in _REQUIRED_CUDA_DLLS
        if not any(os.path.isfile(os.path.join(d, dll)) for d in dirs)
    ]


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
    dirs = _cuda_runtime_dirs()
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for bin_dir in dirs:
        with contextlib.suppress(OSError):
            os.add_dll_directory(bin_dir)
        # CTranslate2 loads cuBLAS/cuDNN via a path that ignores
        # add_dll_directory, so PATH is what actually makes them resolvable.
        if bin_dir not in path_parts:
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
            path_parts.append(bin_dir)
    if missing := _missing_cuda_libs(dirs):
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
    return not _missing_cuda_libs(_cuda_runtime_dirs())


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
    )


__all__ = ["FasterWhisperConfig", "FasterWhisperStt", "create_stt", "manifest"]
