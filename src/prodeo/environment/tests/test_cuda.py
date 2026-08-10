"""CUDA runtime discovery.

Fake install trees under tmp_path are injected, so these run identically on
Linux CI and on a Windows box with a real CUDA.
"""

from pathlib import Path

from prodeo.environment.cuda import CUDA12_RUNTIME, cuda_runtime_dirs, missing_cuda_libraries


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
    )

    assert dirs == [str(new)]  # the v11 tree exists but is never a candidate


def test_cudnn_is_found_outside_the_toolkit_tree(tmp_path: Path) -> None:
    # cuDNN 9 for Windows is a separate installer with its own layout, and its
    # DLLs sit one level deeper under a CUDA-major folder.
    program_files = tmp_path / "Program Files"
    nested = _touch(
        program_files / "NVIDIA" / "CUDNN" / "v9.8" / "bin" / "12.9" / "cudnn_ops64_9.dll"
    ).parent

    dirs = cuda_runtime_dirs(
        env={"ProgramFiles": str(program_files)}, prefix=str(tmp_path / "venv")
    )

    assert dirs == [str(nested), str(nested.parent)]  # deeper layout ranks first
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

    dirs = cuda_runtime_dirs(env={"ProgramFiles": str(program_files)}, prefix=str(prefix))

    assert dirs == [str(toolkit), str(wheel)]
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
