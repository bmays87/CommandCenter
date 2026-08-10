"""TargetDirInstaller: argv construction, containment, and removal.

The subprocess is real - a short python -c stands in for uv/pip - so the
create_subprocess_exec path, exit-code handling, and output capture are
genuinely exercised rather than mocked away.
"""

import sys
from pathlib import Path

import pytest

from prodeo.extensions.installer import (
    TargetDirInstaller,
    _distribution_dir_name,
    _resolve_installer_argv,
    _tail,
)


def _fake_installer(code: str) -> list[str]:
    """An argv prefix that behaves like an installer with a scripted outcome."""
    return [sys.executable, "-c", code]


@pytest.mark.asyncio
async def test_successful_install_reports_command_and_output(tmp_path: Path) -> None:
    script = "import sys; print('Successfully installed'); sys.exit(0)"
    installer = TargetDirInstaller(tmp_path / "lib", argv_fn=lambda: _fake_installer(script))

    result = await installer.install("prodeo-summarizer-ollama")

    assert result.ok is True
    assert result.package == "prodeo-summarizer-ollama"
    assert "Successfully installed" in result.output
    # The target must be created and passed through, or --target installs land
    # in the venv and `uv sync` deletes them.
    assert (tmp_path / "lib").is_dir()
    assert "--target" in result.command
    assert str(tmp_path / "lib") in result.command
    assert result.command[-1] == "prodeo-summarizer-ollama"


@pytest.mark.asyncio
async def test_failed_install_is_contained_not_raised(tmp_path: Path) -> None:
    script = "import sys; print('No matching distribution'); sys.exit(1)"
    installer = TargetDirInstaller(tmp_path / "lib", argv_fn=lambda: _fake_installer(script))

    result = await installer.install("does-not-exist")

    assert result.ok is False
    assert "exited with 1" in result.error
    assert "No matching distribution" in result.output  # the diagnosis survives


@pytest.mark.asyncio
async def test_missing_installer_binary_is_contained(tmp_path: Path) -> None:
    installer = TargetDirInstaller(
        tmp_path / "lib", argv_fn=lambda: ["definitely-not-a-real-binary"]
    )
    result = await installer.install("anything")
    assert result.ok is False and result.error


@pytest.mark.asyncio
async def test_slow_install_times_out_without_hanging_the_server(tmp_path: Path) -> None:
    script = "import time; time.sleep(30)"
    installer = TargetDirInstaller(
        tmp_path / "lib", argv_fn=lambda: _fake_installer(script), timeout_s=0.5
    )

    result = await installer.install("slow-package")

    assert result.ok is False
    assert "timed out" in result.error


@pytest.mark.asyncio
async def test_uninstall_removes_the_distribution_directories(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    (lib / "prodeo_summarizer_ollama").mkdir(parents=True)
    (lib / "prodeo_summarizer_ollama" / "__init__.py").write_text("", encoding="utf-8")
    (lib / "prodeo_summarizer_ollama-0.1.0.dist-info").mkdir()
    (lib / "unrelated_package").mkdir()

    installer = TargetDirInstaller(lib)
    result = await installer.uninstall("prodeo-summarizer-ollama")

    assert result.ok is True
    assert not (lib / "prodeo_summarizer_ollama").exists()
    assert not (lib / "prodeo_summarizer_ollama-0.1.0.dist-info").exists()
    assert (lib / "unrelated_package").exists()  # neighbours untouched


@pytest.mark.asyncio
async def test_uninstall_of_absent_package_reports_failure(tmp_path: Path) -> None:
    result = await TargetDirInstaller(tmp_path / "lib").uninstall("never-installed")
    assert result.ok is False and "nothing installed" in result.error


def test_distribution_dir_name_strips_extras_and_specifiers() -> None:
    assert _distribution_dir_name("prodeo-mjolnir[audio]") == "prodeo_mjolnir"
    assert _distribution_dir_name("prodeo-stt-parakeet>=2.0") == "prodeo_stt_parakeet"
    assert _distribution_dir_name("prodeo-tts-piper==1.2.3") == "prodeo_tts_piper"


def test_resolve_installer_argv_prefers_uv_but_always_returns_something() -> None:
    argv = _resolve_installer_argv()
    # uv on PATH here, but a plain-pip venv (the Pi runbook) must also work.
    # Case-insensitive: shutil.which returns "uv.EXE" on Windows.
    assert argv[-1] == "pip"
    assert argv[0] == sys.executable or argv[0].lower().endswith(("uv", "uv.exe"))


def test_tail_keeps_the_end_where_the_error_is() -> None:
    assert _tail("short") == "short"
    trimmed = _tail("x" * 100 + "THE ACTUAL ERROR", limit=20)
    assert trimmed.startswith("…")
    assert trimmed.endswith("THE ACTUAL ERROR")
