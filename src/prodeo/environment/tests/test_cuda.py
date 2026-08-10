"""CUDA runtime discovery.

Fake install trees under tmp_path are injected, so these run identically on
Linux CI and on a Windows box with a real CUDA.
"""

import sys
from pathlib import Path

import pytest

from prodeo.environment.cuda import (
    CUDA12_RUNTIME,
    cuda_runtime_dirs,
    missing_cuda_libraries,
    registry_install_roots,
)


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

    dirs = cuda_runtime_dirs(
        env={
            "ProgramFiles": str(program_files),
            "CUDA_PATH": str(old.parent),
            "CUDA_PATH_V11_0": str(old.parent),
            "CUDA_PATH_V12_4": str(new.parent),
        },
        prefix=str(tmp_path / "venv"),
        registry_fn=list,
    )

    # The v11 tree exists on disk and is reachable from CUDA_PATH, but must
    # never be offered: it has cublas64_11, not the 12 CTranslate2 links.
    assert str(new) in dirs
    assert not any("v11.0" in d for d in dirs)


def test_cudnn_is_found_outside_the_toolkit_tree(tmp_path: Path) -> None:
    # cuDNN 9 for Windows is a separate installer with its own layout, and its
    # DLLs sit one level deeper under a CUDA-major folder.
    program_files = tmp_path / "Program Files"
    nested = _touch(
        program_files / "NVIDIA" / "CUDNN" / "v9.8" / "bin" / "12.9" / "cudnn_ops64_9.dll"
    ).parent

    dirs = cuda_runtime_dirs(
        env={"ProgramFiles": str(program_files)},
        prefix=str(tmp_path / "venv"),
        registry_fn=list,
    )

    # Deepest layout first, then the shallower fallbacks for older installs.
    assert dirs[0] == str(nested)
    assert missing_cuda_libraries(dirs) == ["cublas64_12.dll"]


def test_pip_wheels_still_resolve_but_rank_last(tmp_path: Path) -> None:
    # Installing the nvidia-* wheels into the venv keeps working; it is just no
    # longer the only thing we look for (uv sync prunes them).
    program_files = tmp_path / "Program Files"
    prefix = tmp_path / "venv"
    toolkit = _touch(_toolkit_bin(program_files, "v12.4") / "cublas64_12.dll").parent
    wheel = _touch(
        prefix / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin" / "cudnn_ops64_9.dll"
    ).parent

    dirs = cuda_runtime_dirs(
        env={"ProgramFiles": str(program_files)}, prefix=str(prefix), registry_fn=list
    )

    assert str(toolkit) in dirs and str(wheel) in dirs
    assert dirs.index(str(toolkit)) < dirs.index(str(wheel))  # wheels rank last
    assert missing_cuda_libraries(dirs) == []


def test_missing_libraries_are_named(tmp_path: Path) -> None:
    bin_dir = _touch(_toolkit_bin(tmp_path, "v12.4") / "cublas64_12.dll").parent
    # The CTranslate2 wheel bundles the cudnn64_9 dispatcher, so a *backend* is
    # what actually goes missing - probing the dispatcher would never fire.
    assert missing_cuda_libraries([str(bin_dir)]) == ["cudnn_ops64_9.dll"]
    assert missing_cuda_libraries([]) == list(CUDA12_RUNTIME)


def test_a_caller_may_ask_for_its_own_libraries(tmp_path: Path) -> None:
    bin_dir = _touch(_toolkit_bin(tmp_path, "v12.4") / "somethingelse.dll").parent
    assert missing_cuda_libraries([str(bin_dir)], ("somethingelse.dll",)) == []


# --- the two bugs that reported a real cuDNN 9.25 as missing -----------------


def test_an_install_on_another_drive_is_found_via_the_registry(tmp_path: Path) -> None:
    # The regression: cuDNN installed to "F:\\Program Files" - which is exactly
    # what this project tells users to do with bulk data - was invisible to a
    # probe rooted at %ProgramFiles%. The registry knows where it went.
    elsewhere = tmp_path / "OtherDrive" / "NVIDIA" / "CUDNN" / "v9.25"
    _touch(elsewhere / "bin" / "12.9" / "x64" / "cudnn_ops64_9.dll")

    dirs = cuda_runtime_dirs(
        env={"ProgramFiles": str(tmp_path / "Program Files")},
        prefix=str(tmp_path / "venv"),
        registry_fn=lambda: [str(elsewhere)],
    )

    assert str(elsewhere / "bin" / "12.9" / "x64") in dirs
    assert missing_cuda_libraries(dirs) == ["cublas64_12.dll"]


def test_the_cudnn_9_25_x64_layout_is_searched(tmp_path: Path) -> None:
    # Second, independent bug: cuDNN 9.25 puts its DLLs in bin/<ver>/x64, one
    # level deeper than the old glob looked - so it was missed even on C:.
    program_files = tmp_path / "Program Files"
    deep = program_files / "NVIDIA" / "CUDNN" / "v9.25" / "bin" / "12.9" / "x64"
    _touch(deep / "cudnn_ops64_9.dll")

    dirs = cuda_runtime_dirs(
        env={"ProgramFiles": str(program_files)},
        prefix=str(tmp_path / "venv"),
        registry_fn=list,
    )

    assert str(deep) in dirs


def test_registry_roots_rank_ahead_of_guessed_paths(tmp_path: Path) -> None:
    program_files = tmp_path / "Program Files"
    guessed = _touch(_toolkit_bin(program_files, "v12.4") / "cublas64_12.dll").parent
    registry = tmp_path / "Elsewhere" / "CUDA" / "v12.9"
    _touch(registry / "bin" / "cublas64_12.dll")

    dirs = cuda_runtime_dirs(
        env={"ProgramFiles": str(program_files)},
        prefix=str(tmp_path / "venv"),
        registry_fn=lambda: [str(registry)],
    )

    # What Windows recorded beats what we guessed.
    assert dirs.index(str(registry / "bin")) < dirs.index(str(guessed))


def test_registry_lookup_is_skipped_off_windows() -> None:
    # winreg does not exist there; the probe must degrade, not explode.
    if sys.platform == "win32":
        pytest.skip("this asserts the non-Windows path")
    assert registry_install_roots() == []
