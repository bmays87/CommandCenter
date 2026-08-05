"""faster-whisper engine wrapper: audio conversion, joining, lazy caching,
CUDA auto-detection and system CUDA-runtime discovery.

The real faster_whisper library is stubbed via sys.modules so no model
weights are needed; the wrapper's own logic is what's under test. The
discovery tests build fake install trees under tmp_path and inject them, so
they run identically on Linux CI and on a Windows box with a real CUDA.
"""

import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy
import pytest

from prodeo_mjolnir.engines import SAMPLE_RATE, AudioClip
from prodeo_stt_fasterwhisper import (
    FasterWhisperConfig,
    FasterWhisperStt,
    _add_cuda_dll_dirs,
    _cuda_runtime_dirs,
    _cuda_runtime_usable,
    _missing_cuda_libs,
    manifest,
)


@dataclass
class Segment:
    text: str


class FakeWhisperModel:
    instances: ClassVar[list["FakeWhisperModel"]] = []
    #: Every constructor kwargs dict, including attempts that then raised.
    attempts: ClassVar[list[dict[str, Any]]] = []
    #: When True, a construction requesting CUDA raises (models broken libs).
    fail_on_cuda: ClassVar[bool] = False
    #: When True, a CUDA model *constructs* fine and raises at transcribe time
    #: instead - CTranslate2's real behavior: cuBLAS/cuDNN load lazily at the
    #: first encode, not in the constructor.
    fail_on_cuda_encode: ClassVar[bool] = False

    def __init__(self, model: str, **kwargs: Any) -> None:
        FakeWhisperModel.attempts.append(kwargs)
        if FakeWhisperModel.fail_on_cuda and kwargs.get("device") == "cuda":
            raise RuntimeError("CUDA driver/runtime error")
        self.model = model
        self.kwargs = kwargs
        self.audio: list[Any] = []
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[list[Segment], object]:
        if FakeWhisperModel.fail_on_cuda_encode and self.kwargs.get("device") == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        self.audio.append(audio)
        return [Segment(" What happened "), Segment("overnight? ")], object()


@pytest.fixture(autouse=True)
def fake_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWhisperModel.instances = []
    FakeWhisperModel.attempts = []
    FakeWhisperModel.fail_on_cuda = False
    FakeWhisperModel.fail_on_cuda_encode = False
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    # Deterministic defaults: no GPU unless a test says otherwise, and don't
    # let the host's real CUDA install (or lack of one) leak into the suite.
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_available", lambda: False)
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_runtime_usable", lambda: True)


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
async def test_auto_selects_cpu_without_gpu() -> None:
    # Default config is device="auto"/compute_type="auto"; no GPU -> cpu/int8.
    stt = FasterWhisperStt(FasterWhisperConfig())
    await stt.transcribe(_clip())
    kwargs = FakeWhisperModel.instances[0].kwargs
    assert kwargs["device"] == "cpu"
    assert kwargs["compute_type"] == "int8"


@pytest.mark.asyncio
async def test_auto_selects_cuda_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_available", lambda: True)
    stt = FasterWhisperStt(FasterWhisperConfig())
    await stt.transcribe(_clip())
    kwargs = FakeWhisperModel.instances[0].kwargs
    assert kwargs["device"] == "cuda"
    assert kwargs["compute_type"] == "float16"  # auto picks fp16 on GPU


@pytest.mark.asyncio
async def test_cuda_load_failure_falls_back_to_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_available", lambda: True)
    FakeWhisperModel.fail_on_cuda = True
    stt = FasterWhisperStt(FasterWhisperConfig())
    text = await stt.transcribe(_clip())
    assert text == "What happened overnight?"  # still transcribed

    devices = [a["device"] for a in FakeWhisperModel.attempts]
    computes = [a["compute_type"] for a in FakeWhisperModel.attempts]
    assert devices == ["cuda", "cpu"]  # tried GPU, fell back
    assert computes == ["float16", "int8"]
    assert len(FakeWhisperModel.instances) == 1  # only the CPU model constructed


@pytest.mark.asyncio
async def test_cuda_encode_failure_falls_back_to_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 2026-07-31 incident: CTranslate2 loads cuBLAS lazily, so the CUDA
    # model *constructs* fine on a machine with no runtime and only raises at
    # the first encode. The load-time probe must surface that and fall back,
    # not let it escape mid-command.
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_available", lambda: True)
    FakeWhisperModel.fail_on_cuda_encode = True
    stt = FasterWhisperStt(FasterWhisperConfig())
    text = await stt.transcribe(_clip())
    assert text == "What happened overnight?"  # still transcribed, on CPU

    devices = [a["device"] for a in FakeWhisperModel.attempts]
    assert devices == ["cuda", "cpu"]  # constructed, probed, fell back


@pytest.mark.asyncio
async def test_auto_skips_cuda_when_runtime_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    # Driver present but cuBLAS/cuDNN missing: auto must go straight to CPU
    # instead of paying a doomed CUDA model load every boot.
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_available", lambda: True)
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_runtime_usable", lambda: False)
    stt = FasterWhisperStt(FasterWhisperConfig())
    await stt.transcribe(_clip())
    kwargs = FakeWhisperModel.instances[0].kwargs
    assert (kwargs["device"], kwargs["compute_type"]) == ("cpu", "int8")
    assert len(FakeWhisperModel.attempts) == 1  # CUDA never attempted


@pytest.mark.asyncio
async def test_explicit_device_overrides_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    # A GPU is present, but an explicit device pin must be honored unchanged.
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_available", lambda: True)
    stt = FasterWhisperStt(FasterWhisperConfig(device="cpu", compute_type="int8"))
    await stt.transcribe(_clip())
    kwargs = FakeWhisperModel.instances[0].kwargs
    assert (kwargs["device"], kwargs["compute_type"]) == ("cpu", "int8")


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


# --- CUDA runtime discovery -------------------------------------------------


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _toolkit_bin(root: Path, version: str) -> Path:
    return root / "NVIDIA GPU Computing Toolkit" / "CUDA" / version / "bin"


def test_unversioned_cuda_path_is_ignored(tmp_path: Path) -> None:
    # CUDA_PATH points at whichever toolkit was installed last, which is
    # routinely an older major with no cublas64_12.dll. Only the explicitly
    # versioned roots may be trusted.
    program_files = tmp_path / "Program Files"
    old = _touch(_toolkit_bin(program_files, "v11.0") / "cublas64_11.dll").parent
    new = _touch(_toolkit_bin(program_files, "v12.4") / "cublas64_12.dll").parent

    dirs = _cuda_runtime_dirs(
        env={
            "ProgramFiles": str(program_files),
            "CUDA_PATH": str(old.parent),
            "CUDA_PATH_V11_0": str(old.parent),
            "CUDA_PATH_V12_4": str(new.parent),
        },
        prefix=str(tmp_path / "venv"),
    )

    assert dirs == [str(new)]  # the v11 tree exists but is never a candidate


def test_cudnn_is_found_outside_the_toolkit_tree(tmp_path: Path) -> None:
    # cuDNN 9 for Windows is a separate installer with its own layout, and its
    # DLLs sit one level deeper under a CUDA-major folder.
    program_files = tmp_path / "Program Files"
    nested = _touch(
        program_files / "NVIDIA" / "CUDNN" / "v9.8" / "bin" / "12.9" / "cudnn_ops64_9.dll"
    ).parent

    dirs = _cuda_runtime_dirs(
        env={"ProgramFiles": str(program_files)}, prefix=str(tmp_path / "venv")
    )

    assert dirs == [str(nested), str(nested.parent)]  # deeper layout ranks first
    assert _missing_cuda_libs(dirs) == ["cublas64_12.dll"]


def test_pip_wheels_still_resolve_but_rank_last(tmp_path: Path) -> None:
    # Installing the nvidia-* wheels into the venv keeps working; it is just no
    # longer the only thing we look for (uv sync prunes them).
    program_files = tmp_path / "Program Files"
    prefix = tmp_path / "venv"
    toolkit = _touch(_toolkit_bin(program_files, "v12.4") / "cublas64_12.dll").parent
    wheel = _touch(
        prefix / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin" / "cudnn_ops64_9.dll"
    ).parent

    dirs = _cuda_runtime_dirs(env={"ProgramFiles": str(program_files)}, prefix=str(prefix))

    assert dirs == [str(toolkit), str(wheel)]
    assert _missing_cuda_libs(dirs) == []


def test_missing_cudnn_is_reported(tmp_path: Path) -> None:
    bin_dir = _touch(_toolkit_bin(tmp_path, "v12.4") / "cublas64_12.dll").parent
    # The CTranslate2 wheel bundles the cudnn64_9 dispatcher, so a *backend* is
    # what actually goes missing - probing for the dispatcher would never fire.
    assert _missing_cuda_libs([str(bin_dir)]) == ["cudnn_ops64_9.dll"]
    assert _missing_cuda_libs([]) == ["cublas64_12.dll", "cudnn_ops64_9.dll"]


def test_add_cuda_dll_dirs_prepends_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _touch(_toolkit_bin(tmp_path, "v12.4") / "cublas64_12.dll").parent
    _touch(bin_dir / "cudnn_ops64_9.dll")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "add_dll_directory", lambda _p: None, raising=False)
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_runtime_dirs", lambda: [str(bin_dir)])
    monkeypatch.setenv("PATH", "/existing")

    _add_cuda_dll_dirs()

    # CTranslate2 resolves cuBLAS/cuDNN through PATH, not add_dll_directory.
    assert os.environ["PATH"].split(os.pathsep) == [str(bin_dir), "/existing"]


def test_cuda_runtime_usable_requires_the_dlls_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    complete = _touch(_toolkit_bin(tmp_path, "v12.4") / "cublas64_12.dll").parent
    _touch(complete / "cudnn_ops64_9.dll")

    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_runtime_dirs", lambda: [str(complete)])
    assert _cuda_runtime_usable() is True
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_runtime_dirs", lambda: [])
    assert _cuda_runtime_usable() is False


def test_cuda_runtime_usable_trusts_the_driver_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No cheap library check off Windows - the driver probe alone decides.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_runtime_dirs", lambda: [])
    assert _cuda_runtime_usable() is True


def test_add_cuda_dll_dirs_is_a_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> list[str]:
        raise AssertionError("must not probe for a Windows CUDA layout off Windows")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("prodeo_stt_fasterwhisper._cuda_runtime_dirs", _boom)

    _add_cuda_dll_dirs()
