"""Building the CCAN installer the dashboard serves (ADR-0025).

Every download is minted for this hub: the zip carries the hub certificate
(the CCAN's trust anchor — the parent-only rule), a fresh single-use
enrollment token, and the unpublished first-party wheels (``prodeo``,
``prodeo-ccan``) built from this checkout with the same ``uv build`` path
the extensions manager uses. Third-party dependencies resolve from PyPI on
the target machine, keeping the artifact small and the installer
platform-agnostic: anything with Python 3.12+ can run it.
"""

import asyncio
import json
import shutil
import zipfile
from importlib import resources
from pathlib import Path

import structlog
from ulid import ULID

from prodeo.errors import InstallerUnavailableError
from prodeo.identity import IdentityProvider
from prodeo.machines.enrollments import Enrollments
from prodeo.machines.pairing import DEFAULT_CCAN_PORT

_log = structlog.get_logger(__name__)

_BUILD_TIMEOUT_S = 600.0


class InstallerBuilder:
    """Assembles one-hub-only CCAN installer zips on demand."""

    def __init__(
        self,
        *,
        workspace: Path | None,
        wheels_dir: Path,
        out_dir: Path,
        identity: IdentityProvider,
        enrollments: Enrollments,
        hub_node: str,
        hub_address_hint: str = "",
    ) -> None:
        self._workspace = workspace
        self._wheels_dir = wheels_dir
        self._out_dir = out_dir
        self._identity = identity
        self._enrollments = enrollments
        self._hub_node = hub_node
        self._hub_address_hint = hub_address_hint

    def unavailable_reason(self) -> str | None:
        """Why no installer can be produced, or None when one can."""
        if self._workspace is None:
            return (
                "this hub is not running from a source checkout, so it cannot "
                "build the unpublished CCAN wheels an installer needs"
            )
        if not _first_party_wheels(self._wheels_dir) and shutil.which("uv") is None:
            return "uv is required to build the CCAN wheels and is not on PATH"
        return None

    async def build(self) -> Path:
        """Mint a token and produce a fresh installer zip; caller owns the file.

        Raises :class:`InstallerUnavailableError` with the reason when this
        hub cannot produce one.
        """
        reason = self.unavailable_reason()
        if reason is not None:
            raise InstallerUnavailableError(reason)
        await self._ensure_wheels()
        wheels = _first_party_wheels(self._wheels_dir)
        identity = await self._identity.get()
        token = await self._enrollments.mint(label="ccan-installer")
        config = {
            "hub": {
                "node": self._hub_node,
                "certificate_pem": identity.certificate_pem,
                "address_hint": self._hub_address_hint,
            },
            "enroll_token": token,
            "port": DEFAULT_CCAN_PORT,
        }
        self._out_dir.mkdir(parents=True, exist_ok=True)
        out = self._out_dir / f"prodeo-ccan-installer-{ULID()}.zip"
        script = (
            resources.files("prodeo.machines")
            .joinpath("installer_files/install.py")
            .read_text(encoding="utf-8")
        )
        await asyncio.to_thread(_write_zip, out, script, config, wheels)
        _log.info("installer.built", path=str(out), wheels=[w.name for w in wheels])
        return out

    async def _ensure_wheels(self) -> None:
        """Build the workspace wheels once; later downloads reuse them."""
        if _first_party_wheels(self._wheels_dir):
            return
        uv = shutil.which("uv")
        if uv is None or self._workspace is None:  # unavailable_reason() covers this
            raise InstallerUnavailableError("uv is required to build the CCAN wheels")
        self._wheels_dir.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            uv,
            "build",
            "--all-packages",
            "-o",
            str(self._wheels_dir),
            cwd=str(self._workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(_BUILD_TIMEOUT_S):
                stdout, _ = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise InstallerUnavailableError(
                f"building the CCAN wheels timed out after {_BUILD_TIMEOUT_S:.0f}s"
            ) from None
        if process.returncode != 0:
            tail = stdout.decode(errors="replace")[-1000:]
            _log.warning("installer.wheel_build_failed", output=tail)
            raise InstallerUnavailableError(f"building the CCAN wheels failed: {tail}")
        if not _first_party_wheels(self._wheels_dir):
            raise InstallerUnavailableError("the workspace build produced no prodeo-ccan wheel")


def _first_party_wheels(wheels_dir: Path) -> list[Path]:
    """The wheels an installer must bundle, or nothing when incomplete.

    Core + the node daemon, plus the first-party claude-code adapter so a
    fresh machine can observe and launch agents out of the box (ADR-0026).
    ``prodeo-*.whl`` matches only the core wheel — every other first-party
    distribution normalizes to ``prodeo_<name>``.
    """
    if not wheels_dir.is_dir():
        return []
    core = sorted(wheels_dir.glob("prodeo-*.whl"))
    ccan = sorted(wheels_dir.glob("prodeo_ccan-*.whl"))
    adapter = sorted(wheels_dir.glob("prodeo_adapter_claude_code-*.whl"))
    if not core or not ccan:
        return []
    return [core[-1], ccan[-1], *adapter[-1:]]


def _write_zip(out: Path, script: str, config: dict[str, object], wheels: list[Path]) -> None:
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("install.py", script)
        zf.writestr("ccan.json", json.dumps(config, indent=2))
        for wheel in wheels:
            zf.write(wheel, f"wheels/{wheel.name}")
