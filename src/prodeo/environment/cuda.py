"""Finding a usable CUDA runtime on Windows.

CUDA is a machine-wide prerequisite, not a Python dependency: pip's ``nvidia-*``
wheels live in the virtualenv and the next ``uv sync`` deletes them, so the
engines look for a system install instead.

**The registry is the authority.** An earlier version globbed
``%ProgramFiles%\\NVIDIA\\CUDNN`` and reported a fully-installed cuDNN 9.25 as
missing, because it had been installed to ``F:\\Program Files`` - exactly what
this project tells users to do with bulk data. Windows records where each
component actually went; ask it before guessing at paths.

This lives in core rather than in the speech engine that first needed it, so
the environment view and the engine agree about what "CUDA is installed" means.
The dependency runs the right way round - engines already depend on ``prodeo``.
"""

import glob
import os
import sys
from collections.abc import Callable, Mapping

import structlog

_log = structlog.get_logger(__name__)

#: The libraries the bundled speech engines actually dlopen. Version-specific:
#: CTranslate2 4.x links CUDA **12** (not 13), and the cuDNN 9 dispatcher it
#: ships needs backends it does not. Verified against the import table of
#: ``ctranslate2.dll``; re-check with a string dump rather than guessing.
CUDA12_RUNTIME: tuple[str, ...] = ("cublas64_12.dll", "cudnn_ops64_9.dll")

#: How to obtain each one, for the fix hints. The winget default is CUDA 13.x,
#: which does not work.
CUDA_INSTALL_HINT = "winget install --id Nvidia.CUDA -e --version 12.9"
CUDNN_DOWNLOAD_URL = "https://developer.nvidia.com/cudnn-downloads"

#: Uninstall entries whose ``InstallLocation`` points at a CUDA/cuDNN tree.
_REGISTRY_NAME_HINTS = ("cudnn", "cuda toolkit", "cudart", "cuda runtime")

_UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)

RegistryRootsFn = Callable[[], list[str]]


def registry_install_roots() -> list[str]:
    """Install directories Windows recorded for CUDA and cuDNN components.

    Drive-agnostic by construction, which is the whole point: a user who put
    cuDNN on D: or F: is doing the right thing and must still be found.
    Returns an empty list off Windows or when the registry is unreadable.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # not Windows, or a stripped build
        return []

    roots: list[str] = []
    for path in _UNINSTALL_KEYS:
        try:
            uninstall = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            continue
        with uninstall:
            for index in range(winreg.QueryInfoKey(uninstall)[0]):
                try:
                    name = winreg.EnumKey(uninstall, index)
                    with winreg.OpenKey(uninstall, name) as entry:
                        display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                        if not any(h in display.lower() for h in _REGISTRY_NAME_HINTS):
                            continue
                        location = str(winreg.QueryValueEx(entry, "InstallLocation")[0]).strip()
                except OSError:
                    continue  # entries without these values are the common case
                if location:
                    roots.append(location)
    return roots


def _bin_dirs(root: str) -> list[str]:
    """The plausible library directories inside one install root.

    Three shapes are in the wild and all are cheap to check:
    ``<root>/bin`` (CUDA toolkit), ``<root>/bin/12.9`` (older cuDNN), and
    ``<root>/bin/12.9/x64`` (cuDNN 9.25). Bounded - never a recursive walk.
    """
    return [
        *glob.glob(os.path.join(root, "bin", "*", "x64")),
        *glob.glob(os.path.join(root, "bin", "*")),
        os.path.join(root, "bin"),
        root,
    ]


def cuda_runtime_dirs(
    env: Mapping[str, str] | None = None,
    prefix: str | None = None,
    registry_fn: RegistryRootsFn = registry_install_roots,
) -> list[str]:
    """Directories that may hold the CUDA runtime, best candidate first.

    ``env``/``prefix``/``registry_fn`` are injectable so tests don't touch the
    real filesystem or registry.
    """
    environ = os.environ if env is None else env
    py_prefix = sys.prefix if prefix is None else prefix
    program_files = environ.get("ProgramFiles", r"C:\Program Files")
    roots: list[str] = []

    # 1. What Windows says it installed, wherever it put it.
    roots += registry_fn()

    # 2. Explicitly-versioned environment variables. Bare CUDA_PATH is *not*
    #    consulted: it points at whichever toolkit was installed last, which is
    #    routinely an older major (e.g. v11.0) with no cublas64_12.dll.
    for name, value in environ.items():
        if name.startswith(("CUDA_PATH_V12_", "CUDA_PATH_V13_")):
            roots.append(value)
    if cudnn_path := environ.get("CUDNN_PATH"):
        roots.append(cudnn_path)

    # 3. Default locations, for installs the registry does not describe.
    roots += glob.glob(
        os.path.join(program_files, "NVIDIA GPU Computing Toolkit", "CUDA", "v1[23].*")
    )
    roots += glob.glob(os.path.join(program_files, "NVIDIA", "CUDNN", "v9.*"))

    candidates: list[str] = []
    for root in roots:
        candidates += _bin_dirs(root)

    # 4. Last resort: the pip wheels, if someone installed them into this venv.
    #    Still supported, but it is the fragile path - see the module docstring.
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
