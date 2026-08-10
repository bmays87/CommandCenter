"""Environment checks: what each probe reports, and that none can blow up."""

from pathlib import Path

import pytest

from prodeo.environment import Environment, report
from prodeo.environment.checks import _audio, _disk, _gpu, _gpu_stt


def _env(**overrides: object) -> Environment:
    base: dict[str, object] = {
        "platform": "linux",
        "python": (3, 12),
        "nvidia_gpu": False,
        "details": {"python_version": "3.12.0"},
    }
    base.update(overrides)
    return Environment.model_validate(base)


def test_gpu_absence_is_informational_not_a_failure() -> None:
    check = _gpu(_env(nvidia_gpu=False), "")
    # An AMD or Intel GPU is invisible to the cheap NVIDIA probe but still
    # works via DirectML, so this must never read as "you have a problem".
    assert check.relevant is False
    assert "Not required" in check.fix
    assert _gpu(_env(nvidia_gpu=True), "").ok is True


def _providers(monkeypatch: pytest.MonkeyPatch, available: list[str]) -> None:
    monkeypatch.setattr("prodeo.environment.checks._onnx_providers", lambda: available)


def _no_cuda_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "prodeo.environment.checks.missing_cuda_libraries", lambda *_: ["cublas64_12.dll"]
    )


def test_directml_alone_counts_as_a_gpu_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point of the reframe: DirectML needs no CUDA, so a machine with
    # it is GPU-capable even though every CUDA library is absent.
    _providers(monkeypatch, ["DmlExecutionProvider", "CPUExecutionProvider"])
    _no_cuda_runtime(monkeypatch)

    check = _gpu_stt(_env(platform="win32"), "")

    assert check.ok is True
    assert "DirectML" in check.detail


def test_ctranslate2_cuda_alone_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    _providers(monkeypatch, ["CPUExecutionProvider"])
    monkeypatch.setattr("prodeo.environment.checks.missing_cuda_libraries", lambda *_: [])

    check = _gpu_stt(_env(platform="win32"), "")

    assert check.ok is True
    assert "faster-whisper" in check.detail


def test_every_available_route_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    _providers(monkeypatch, ["DmlExecutionProvider", "CUDAExecutionProvider"])
    monkeypatch.setattr("prodeo.environment.checks.missing_cuda_libraries", lambda *_: [])

    detail = _gpu_stt(_env(platform="win32"), "").detail

    assert "DirectML" in detail and "faster-whisper" in detail
    assert "onnxruntime" in detail


def test_no_route_offers_directml_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _providers(monkeypatch, ["CPUExecutionProvider"])
    _no_cuda_runtime(monkeypatch)

    check = _gpu_stt(_env(platform="win32"), "")

    assert check.ok is False
    # The cheap route is the offered command; CUDA is mentioned but not the
    # thing we tell the user to run.
    assert check.fix_command == "uv pip install onnxruntime-directml"
    assert "DirectML" in check.fix and "cuDNN 9" in check.fix
    # And it must not imply the GPU is necessary at all.
    assert "CPU is fast enough" in check.fix


def test_gpu_stt_is_not_guessed_at_off_windows() -> None:
    check = _gpu_stt(_env(nvidia_gpu=True, platform="linux"), "")
    assert check.relevant is False
    assert "not checked" in check.detail


def test_a_missing_onnxruntime_does_not_break_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Core must not require an engine dependency to answer questions about
    # the host.
    _providers(monkeypatch, [])
    _no_cuda_runtime(monkeypatch)
    assert _gpu_stt(_env(platform="win32"), "").ok is False


def test_disk_reports_free_space_and_warns_when_low(tmp_path: Path) -> None:
    check = _disk(_env(), str(tmp_path))
    assert "GB free" in check.detail
    assert check.relevant is True


def test_disk_walks_up_to_a_directory_that_exists(tmp_path: Path) -> None:
    # The models dir usually does not exist yet when the user is deciding
    # where to put it, so probe the nearest existing parent instead.
    check = _disk(_env(), str(tmp_path / "not" / "created" / "yet"))
    assert check.ok is True
    assert str(tmp_path) in check.detail


def test_disk_is_skipped_when_no_directory_is_set() -> None:
    assert _disk(_env(), "").relevant is False


def test_audio_reports_something_either_way() -> None:
    # sounddevice may or may not be importable here; both paths must produce a
    # usable answer rather than raising.
    check = _audio(_env(), "")
    assert check.id == "audio"
    assert check.detail


@pytest.mark.asyncio
async def test_report_covers_every_check_and_never_raises(tmp_path: Path) -> None:
    result = await report(models_dir=str(tmp_path), env=_env())
    ids = {c.id for c in result.checks}
    assert ids == {"gpu", "gpu_stt", "audio", "disk", "ollama"}
    assert result.platform == "linux"
    assert result.python == "3.12.0"


@pytest.mark.asyncio
async def test_a_check_that_raises_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    # This page exists for machines where something is wrong; one bad probe
    # must not blank it.
    def explodes(_host: Environment, _models_dir: str) -> object:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("prodeo.environment.checks._gpu", explodes)
    result = await report(env=_env())
    broken = next(c for c in result.checks if not c.ok and "exploded" in c.detail)
    assert broken.detail.startswith("check failed")
    # The others still ran.
    assert {"gpu_stt", "audio", "disk", "ollama"} <= {c.id for c in result.checks}
