"""What this machine can actually run.

Extensions declare requirements (an NVIDIA GPU, a minimum Python, a platform);
this is where those claims are checked against reality, so a user learns that
Parakeet needs a GPU *before* a multi-gigabyte download rather than after.

Everything is cheap and side-effect free - no subprocesses, no network - so it
is safe to call while serving a request. The whole surface is a single
:class:`Environment` value object built by :func:`detect`, which means tests
construct one directly and never depend on the host they run on.
"""

import platform
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel, Field

#: Present on any Linux box with the NVIDIA kernel driver loaded, including
#: ones where nvidia-smi was never installed.
_LINUX_NVIDIA_DRIVER = Path("/proc/driver/nvidia/version")


class Environment(BaseModel):
    """A snapshot of the host, as extension requirements see it."""

    #: ``sys.platform``: ``win32`` / ``linux`` / ``darwin``.
    platform: str
    #: ``(major, minor)`` of the interpreter running the server.
    python: tuple[int, int]
    #: An NVIDIA GPU appears to be present. Says nothing about CUDA being
    #: installed - that is a separate, and much more commonly missing, thing.
    nvidia_gpu: bool = False
    #: Free-form details for the environment view; not used for gating.
    details: dict[str, str] = Field(default_factory=dict)

    def python_at_least(self, minimum: str) -> bool:
        """Whether the interpreter satisfies a ``"3.10"``-style floor."""
        try:
            wanted = tuple(int(part) for part in minimum.split(".")[:2])
        except ValueError:
            return True  # an unparsable requirement gates nothing
        return self.python >= wanted


def has_nvidia_gpu() -> bool:
    """Best-effort NVIDIA detection without spawning anything.

    ``nvidia-smi`` on PATH is the near-universal signal on Windows and Linux;
    the driver file catches Linux hosts that have the driver but not the
    utility. A false negative costs a warning the user can override by
    installing by hand, so erring quiet is the right failure direction.
    """
    if shutil.which("nvidia-smi") is not None:
        return True
    try:
        return _LINUX_NVIDIA_DRIVER.exists()
    except OSError:
        return False


def detect() -> Environment:
    """Read the current host into an :class:`Environment`."""
    return Environment(
        platform=sys.platform,
        python=(sys.version_info.major, sys.version_info.minor),
        nvidia_gpu=has_nvidia_gpu(),
        details={
            "python_version": platform.python_version(),
            "machine": platform.machine(),
        },
    )
