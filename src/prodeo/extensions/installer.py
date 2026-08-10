"""Installing and removing extension packages.

The first thing the server does on the user's behalf that executes code, so the
surface is deliberately narrow: the API only ever passes a package spec that it
resolved from the sanctioned catalog (ADR-0015), and everything here runs as an
argv list through ``asyncio.create_subprocess_exec`` - never a shell.

``uv`` is preferred because it is the project's tool and is far faster, but it
cannot be assumed: the satellite runbook installs into a plain venv with plain
pip, so ``python -m pip`` is the fallback.
"""

import asyncio
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import BaseModel

_log = structlog.get_logger(__name__)

#: Bound on one install. Model-sized wheels over a slow link are the worst
#: case; beyond this something is wrong and the user deserves to be told.
DEFAULT_TIMEOUT_S = 900.0


class InstallResult(BaseModel):
    """What an install or uninstall attempt did."""

    ok: bool
    package: str
    #: The argv actually run, so a failure can be reproduced by hand.
    command: list[str] = []
    #: Combined stdout/stderr, trimmed - enough to diagnose, not to flood.
    output: str = ""
    error: str = ""


class PackageInstaller(Protocol):
    """Installs and removes extension distributions."""

    async def install(self, package: str) -> InstallResult: ...

    async def uninstall(self, package: str) -> InstallResult: ...


def _resolve_installer_argv() -> list[str]:
    """``uv pip`` when available, else this interpreter's pip."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip"]
    return [sys.executable, "-m", "pip"]


class TargetDirInstaller:
    """Installs into a directory on ``sys.path``, outside the virtualenv.

    ``--target`` keeps user-installed extensions clear of ``.venv``, so
    ``uv sync`` cannot remove them (see :mod:`prodeo.extensions.paths`).
    """

    def __init__(
        self,
        target: Path,
        *,
        argv_fn: Callable[[], list[str]] = _resolve_installer_argv,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        find_links: Path | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._target = target
        self._argv_fn = argv_fn
        self._timeout_s = timeout_s
        self._find_links = find_links
        self._workspace = workspace

    async def install(self, package: str) -> InstallResult:
        self._target.mkdir(parents=True, exist_ok=True)
        await self._ensure_local_index()
        argv = [*self._base_argv(), "install", "--target", str(self._target), "--upgrade"]
        if self._find_links is not None and _has_wheels(self._find_links):
            # Unpublished first-party packages resolve from locally built
            # wheels; published ones still come from the index as normal.
            argv += ["--find-links", str(self._find_links)]
        return await self._run([*argv, package], package)

    async def _ensure_local_index(self) -> None:
        """Build the workspace once, on the first install from a checkout.

        A build failure is not fatal: a published package still resolves from
        the index, so we log and let the install speak for itself.
        """
        if self._workspace is None or self._find_links is None:
            return
        if _has_wheels(self._find_links):
            return
        result = await self.build_local_index()
        if not result.ok:
            _log.warning("extensions.local_index_build_failed", error=result.error)

    async def build_local_index(self) -> InstallResult:
        """Build every workspace package into the local wheel index.

        Only meaningful in a repo checkout: it is what makes installing an
        unpublished first-party extension possible at all. A no-op elsewhere.
        """
        if self._workspace is None or self._find_links is None:
            return InstallResult(
                ok=False,
                package="workspace",
                error="not running from a workspace checkout; nothing to build",
            )
        uv = shutil.which("uv")
        if uv is None:
            return InstallResult(
                ok=False, package="workspace", error="uv is required to build local wheels"
            )
        self._find_links.mkdir(parents=True, exist_ok=True)
        return await self._run(
            [uv, "build", "--all-packages", "-o", str(self._find_links)],
            "workspace",
            cwd=self._workspace,
        )

    async def uninstall(self, package: str) -> InstallResult:
        # Neither `uv pip uninstall` nor `pip uninstall` understands --target,
        # so removal is by hand: drop the distribution's own directories. This
        # is exactly what pip's --target install produced.
        name = _distribution_dir_name(package)
        removed: list[str] = []
        try:
            for path in sorted(self._target.glob(f"{name}*")):
                if path.is_dir():
                    await asyncio.to_thread(shutil.rmtree, path)
                else:
                    await asyncio.to_thread(path.unlink)
                removed.append(path.name)
        except OSError as exc:
            _log.exception("extensions.uninstall_failed", package=package)
            return InstallResult(ok=False, package=package, error=str(exc))
        if not removed:
            return InstallResult(
                ok=False,
                package=package,
                error=f"nothing installed for {package!r} in {self._target}",
            )
        return InstallResult(ok=True, package=package, output="removed " + ", ".join(removed))

    def _base_argv(self) -> list[str]:
        return list(self._argv_fn())

    async def _run(
        self, argv: list[str], package: str, *, cwd: Path | None = None
    ) -> InstallResult:
        _log.info("extensions.install_started", package=package, command=argv)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=None if cwd is None else str(cwd),
            )
        except OSError as exc:  # installer binary missing or not executable
            _log.exception("extensions.install_failed", package=package)
            return InstallResult(ok=False, package=package, command=argv, error=str(exc))

        try:
            async with asyncio.timeout(self._timeout_s):
                stdout, _ = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            return InstallResult(
                ok=False,
                package=package,
                command=argv,
                error=f"timed out after {self._timeout_s:.0f}s",
            )

        output = _tail(stdout.decode(errors="replace"))
        if process.returncode != 0:
            _log.warning("extensions.install_failed", package=package, code=process.returncode)
            return InstallResult(
                ok=False,
                package=package,
                command=argv,
                output=output,
                error=f"installer exited with {process.returncode}",
            )
        _log.info("extensions.install_finished", package=package)
        return InstallResult(ok=True, package=package, command=argv, output=output)


def _has_wheels(directory: Path) -> bool:
    return directory.is_dir() and any(directory.glob("*.whl"))


def _distribution_dir_name(package: str) -> str:
    """The on-disk prefix a distribution's files share.

    ``prodeo-mjolnir[audio]>=0.1`` -> ``prodeo_mjolnir``: extras and version
    specifiers are not part of the installed directory name, and pip normalizes
    dashes to underscores in ``.dist-info``.
    """
    name = package.split("[", 1)[0]
    for sep in ("==", ">=", "<=", "~=", ">", "<", "!="):
        name = name.split(sep, 1)[0]
    return name.strip().replace("-", "_")


def _tail(text: str, limit: int = 4000) -> str:
    """Keep the end of installer output - the error is always at the bottom."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…\n" + text[-limit:]
