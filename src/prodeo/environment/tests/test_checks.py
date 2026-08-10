"""Environment checks: what each probe reports, and that none can blow up."""

from pathlib import Path

import pytest

from prodeo.environment import Environment, report
from prodeo.environment.checks import _audio, _cuda, _disk, _gpu


def _env(**overrides: object) -> Environment:
    base: dict[str, object] = {
        "platform": "linux",
        "python": (3, 12),
        "nvidia_gpu": False,
        "details": {"python_version": "3.12.0"},
    }
    base.update(overrides)
    return Environment.model_validate(base)


def test_gpu_absence_is_reported_as_a_downgrade_not_a_failure() -> None:
    check = _gpu(_env(nvidia_gpu=False), "")
    assert check.ok is False
    # A machine without a GPU is not broken; it is slower.
    assert "CPU" in check.fix
    assert _gpu(_env(nvidia_gpu=True), "").ok is True


def test_cuda_is_not_applicable_without_a_gpu() -> None:
    check = _cuda(_env(nvidia_gpu=False), "")
    # ok=True with relevant=False: nothing is wrong, the question just does
    # not apply. Reporting it as a failure would be noise on every CPU box.
    assert (check.ok, check.relevant) == (True, False)


def test_cuda_is_not_guessed_at_off_windows() -> None:
    check = _cuda(_env(nvidia_gpu=True, platform="linux"), "")
    assert check.relevant is False
    assert "not checked" in check.detail


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
    assert ids == {"gpu", "cuda", "audio", "disk", "ollama"}
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
    assert {"cuda", "audio", "disk", "ollama"} <= {c.id for c in result.checks}
