"""What this machine is missing, and what to do about it.

The roadmap's "proactive environment provisioning": detect hardware and
optional dependencies and say what would make things better, instead of
silently degrading and leaving the user to find out from a stack trace.

Every check is cheap and read-only. Failures are contained per check - one
probe that raises must not blank the whole page, since the page exists
precisely for machines where something is wrong.
"""

import shutil
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, Field

from prodeo.environment.cuda import (
    CUDA_INSTALL_HINT,
    CUDNN_DOWNLOAD_URL,
    cuda_runtime_dirs,
    missing_cuda_libraries,
)
from prodeo.environment.probe import Environment, detect

_log = structlog.get_logger(__name__)

#: Ollama's documented default (ADR-0013). Reading it from the ollama plugin's
#: saved config would be tidier, but this view's whole job is to report on a
#: machine where things may not be configured yet.
OLLAMA_URL = "http://127.0.0.1:11434"

#: Below this, a model download is likely to fail partway.
LOW_DISK_GB = 5.0


class EnvironmentCheck(BaseModel):
    """One thing that is either fine, or fixable."""

    id: str
    label: str
    ok: bool
    #: What was found. Always populated, including when ``ok``.
    detail: str = ""
    #: Present only when something can be done about it.
    fix: str = ""
    #: A command the user can copy, when one exists.
    fix_command: str = ""
    #: False means "not applicable here", not "broken" - e.g. CUDA on a
    #: machine with no NVIDIA GPU at all.
    relevant: bool = True


class EnvironmentReport(BaseModel):
    platform: str
    python: str
    checks: list[EnvironmentCheck] = Field(default_factory=list)


async def report(models_dir: str = "", env: Environment | None = None) -> EnvironmentReport:
    """Probe the host. Never raises; a broken check reports itself."""
    host = env or detect()
    checks: list[EnvironmentCheck] = []
    for probe in (_gpu, _gpu_stt, _audio, _disk):
        try:
            checks.append(probe(host, models_dir))
        except Exception as exc:  # a probe must not take the page down
            _log.exception("environment.check_failed", check=probe.__name__)
            checks.append(
                EnvironmentCheck(
                    id=probe.__name__.strip("_"),
                    label=probe.__name__.strip("_"),
                    ok=False,
                    detail=f"check failed: {exc}",
                )
            )
    checks.append(await _ollama())
    return EnvironmentReport(
        platform=host.platform,
        python=host.details.get("python_version", ""),
        checks=checks,
    )


def _gpu(host: Environment, _models_dir: str) -> EnvironmentCheck:
    # Only NVIDIA is detectable cheaply. A DirectML-capable AMD or Intel GPU
    # will read as "none detected" here while still working, so this is
    # informational and never the thing that gates anything.
    return EnvironmentCheck(
        id="gpu",
        label="NVIDIA GPU",
        ok=host.nvidia_gpu,
        relevant=host.nvidia_gpu,
        detail="detected" if host.nvidia_gpu else "none detected",
        fix=""
        if host.nvidia_gpu
        else "Not required. DirectML drives any DX12 GPU, and CPU works too.",
    )


def _onnx_providers() -> list[str]:
    """Execution providers the installed onnxruntime exposes.

    Imported here and contained: onnxruntime is an engine dependency, and core
    must not require it to answer questions about the host.
    """
    try:
        import onnxruntime

        return list(onnxruntime.get_available_providers())
    except Exception:
        return []


def _gpu_stt(host: Environment, _models_dir: str) -> EnvironmentCheck:
    """Whether *any* GPU route for speech-to-text exists.

    Framed around the goal rather than one implementation of it: there are two
    unrelated routes, and reporting only the CUDA one made a 3GB toolkit look
    mandatory when it is not.

    - **DirectML** (`onnxruntime-directml`) drives Parakeet on any DX12 GPU
      with no CUDA at all. The cheap option, Windows only.
    - **CUDA** is required for faster-whisper, whose CTranslate2 backend has no
      other GPU path, and is also usable by Parakeet via `onnxruntime-gpu`.

    Piper is deliberately absent: its upstream API exposes only ``use_cuda``,
    so TTS has no DirectML route. Irrelevant in practice for a 63MB voice.
    """
    if host.platform != "win32":
        # DirectML is Windows-only, and a Linux CUDA resolves through ldconfig
        # which we do not probe. Claiming a result would be a guess.
        return EnvironmentCheck(
            id="gpu_stt",
            label="GPU speech-to-text",
            ok=True,
            relevant=False,
            detail="not checked on this platform",
        )

    providers = _onnx_providers()
    routes: list[str] = []
    if "DmlExecutionProvider" in providers:
        routes.append("DirectML (parakeet)")
    if "CUDAExecutionProvider" in providers:
        routes.append("CUDA via onnxruntime (parakeet)")
    missing = missing_cuda_libraries(cuda_runtime_dirs())
    if not missing:
        routes.append("CUDA (faster-whisper)")

    if routes:
        return EnvironmentCheck(
            id="gpu_stt", label="GPU speech-to-text", ok=True, detail=", ".join(routes)
        )
    return EnvironmentCheck(
        id="gpu_stt",
        label="GPU speech-to-text",
        ok=False,
        detail="no GPU execution path installed - transcription runs on the CPU",
        fix=(
            "Optional; CPU is fast enough for short commands. The cheap upgrade "
            "is DirectML, which needs no CUDA and works on any DX12 GPU - then "
            "set the STT engine to parakeet with "
            'providers ["DmlExecutionProvider"]. Note it replaces the stock '
            f"onnxruntime. {_cuda_gap(missing)}"
        ),
        fix_command="uv pip install onnxruntime-directml",
    )


def _cuda_gap(missing: list[str]) -> str:
    """What faster-whisper's CUDA route still needs, naming only what is absent.

    Telling someone to install cuDNN when they already have it is how you get
    ignored - and it happened, because discovery used to miss a cuDNN on a
    non-system drive.
    """
    needs_cublas = "cublas64_12.dll" in missing
    needs_cudnn = any(name.startswith("cudnn") for name in missing)
    if needs_cublas and needs_cudnn:
        return (
            "faster-whisper on the GPU additionally needs CUDA 12 and cuDNN 9 "
            f"machine-wide ({CUDA_INSTALL_HINT}; cuDNN: {CUDNN_DOWNLOAD_URL})."
        )
    if needs_cublas:
        return (
            "cuDNN 9 is already installed; faster-whisper on the GPU needs only "
            f"the CUDA 12 toolkit ({CUDA_INSTALL_HINT}) - note 12.x, not the "
            "13.x winget default."
        )
    if needs_cudnn:
        return (
            "CUDA 12 is already installed; faster-whisper on the GPU needs only "
            f"cuDNN 9 ({CUDNN_DOWNLOAD_URL})."
        )
    return ""


def _audio(host: Environment, _models_dir: str) -> EnvironmentCheck:
    try:
        import sounddevice
    except Exception:
        return EnvironmentCheck(
            id="audio",
            label="Audio devices",
            ok=False,
            detail="sounddevice is not installed",
            fix="Voice needs PortAudio bindings.",
            fix_command="uv pip install 'prodeo-mjolnir[audio]'",
        )
    devices = sounddevice.query_devices()
    inputs = sum(1 for d in devices if d.get("max_input_channels", 0) > 0)
    outputs = sum(1 for d in devices if d.get("max_output_channels", 0) > 0)
    ok = inputs > 0 and outputs > 0
    return EnvironmentCheck(
        id="audio",
        label="Audio devices",
        ok=ok,
        detail=f"{inputs} input, {outputs} output",
        fix="" if ok else "Voice needs both a microphone and an output device.",
    )


def _disk(_host: Environment, models_dir: str) -> EnvironmentCheck:
    if not models_dir:
        return EnvironmentCheck(
            id="disk", label="Model storage", ok=True, relevant=False, detail="no directory set"
        )
    path = Path(models_dir)
    probe = next((p for p in [path, *path.parents] if p.exists()), None)
    if probe is None:
        return EnvironmentCheck(
            id="disk", label="Model storage", ok=False, detail=f"{models_dir} is unreachable"
        )
    free_gb = shutil.disk_usage(probe).free / 1024**3
    ok = free_gb >= LOW_DISK_GB
    return EnvironmentCheck(
        id="disk",
        label="Model storage",
        ok=ok,
        detail=f"{free_gb:.1f} GB free at {probe}",
        fix="" if ok else "Models run to hundreds of megabytes; choose a drive with room.",
    )


async def _ollama() -> EnvironmentCheck:
    """Reachability of the local LLM that gives Mjölnir its understanding."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
        models = len(response.json().get("models", []))
        return EnvironmentCheck(
            id="ollama",
            label="Local LLM (Ollama)",
            ok=True,
            detail=f"reachable, {models} model{'s' if models != 1 else ''} pulled",
        )
    except Exception:
        return EnvironmentCheck(
            id="ollama",
            label="Local LLM (Ollama)",
            ok=False,
            detail=f"not reachable at {OLLAMA_URL}",
            fix=(
                "Optional. Without it the voice client falls back to its "
                "deterministic grammar - basic commands work, natural language "
                "does not."
            ),
            fix_command="ollama serve",
        )
