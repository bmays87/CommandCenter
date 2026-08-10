"""Finding a usable CUDA runtime on Windows.

CUDA is a machine-wide prerequisite, not a Python dependency: pip's ``nvidia-*``
wheels live in the virtualenv and the next ``uv sync`` deletes them, so the
engines look for a system install instead.

This lives in core rather than in the speech engine that first needed it, so
the environment view and the engine agree about what "CUDA is installed" means.
The dependency runs the right way round - engines already depend on ``prodeo``.
"""

import glob
import os
import sys
from collections.abc import Mapping

#: The libraries the bundled speech engines actually dlopen. Version-specific:
#: CTranslate2 4.x links CUDA **12** (not 13), and the cuDNN 9 dispatcher it
#: ships needs backends it does not. Verified against the import table of
#: ``ctranslate2.dll``; re-check with a string dump rather than guessing.
CUDA12_RUNTIME: tuple[str, ...] = ("cublas64_12.dll", "cudnn_ops64_9.dll")

#: How to obtain it. The winget default is 13.x, which does not work.
CUDA_INSTALL_HINT = "winget install --id Nvidia.CUDA -e --version 12.9"
CUDNN_DOWNLOAD_URL = "https://developer.nvidia.com/cudnn-downloads"


def cuda_runtime_dirs(env: Mapping[str, str] | None = None, prefix: str | None = None) -> list[str]:
    """Directories that may hold the CUDA runtime, best candidate first.

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
    # Still supported, but it is the fragile path - see the module docstring.
    candidates += glob.glob(os.path.join(py_prefix, "Lib", "site-packages", "nvidia", "*", "bin"))

    seen: set[str] = set()
    found: list[str] = []
    for directory in candidates:
        key = os.path.normcase(directory)
        if key not in seen and os.path.isdir(directory):
            seen.add(key)
            found.append(directory)
    return found


def missing_cuda_libraries(
    dirs: list[str], required: tuple[str, ...] = CUDA12_RUNTIME
) -> list[str]:
    """Which required libraries are in none of ``dirs``."""
    return [
        name for name in required if not any(os.path.isfile(os.path.join(d, name)) for d in dirs)
    ]
