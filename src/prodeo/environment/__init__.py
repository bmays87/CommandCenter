"""Host capability detection: what this machine can actually run."""

from prodeo.environment.checks import EnvironmentCheck, EnvironmentReport, report
from prodeo.environment.cuda import cuda_runtime_dirs, missing_cuda_libraries
from prodeo.environment.probe import Environment, detect, has_nvidia_gpu

__all__ = [
    "Environment",
    "EnvironmentCheck",
    "EnvironmentReport",
    "cuda_runtime_dirs",
    "detect",
    "has_nvidia_gpu",
    "missing_cuda_libraries",
    "report",
]
