"""Installer builder: zip contents, minted tokens, unavailability reasons."""

import json
import zipfile
from pathlib import Path

import pytest

from prodeo.errors import InstallerUnavailableError
from prodeo.identity import IdentityProvider
from prodeo.machines.enrollments import Enrollments
from prodeo.machines.installers import InstallerBuilder
from prodeo.machines.pairing import DEFAULT_CCAN_PORT


def _seed_wheels(wheels_dir: Path) -> None:
    """Pretend the workspace build already ran (the real one is uv-slow)."""
    wheels_dir.mkdir(parents=True)
    (wheels_dir / "prodeo-0.1.0-py3-none-any.whl").write_bytes(b"core")
    (wheels_dir / "prodeo_ccan-0.1.0-py3-none-any.whl").write_bytes(b"ccan")
    # A sibling package must never be mistaken for the core wheel.
    (wheels_dir / "prodeo_adapter_claude_code-0.1.0-py3-none-any.whl").write_bytes(b"x")


def _builder(tmp_path: Path, *, workspace: Path | None) -> tuple[InstallerBuilder, Enrollments]:
    enrollments = Enrollments(tmp_path / "enrollments.json")
    builder = InstallerBuilder(
        workspace=workspace,
        wheels_dir=tmp_path / "wheels",
        out_dir=tmp_path / "out",
        identity=IdentityProvider(tmp_path / "identity", common_name="hub"),
        enrollments=enrollments,
        hub_node="hub",
        hub_address_hint="https://hub.example",
    )
    return builder, enrollments


@pytest.mark.asyncio
async def test_build_produces_a_paired_installer(tmp_path: Path) -> None:
    _seed_wheels(tmp_path / "wheels")
    builder, enrollments = _builder(tmp_path, workspace=tmp_path)

    out = await builder.build()

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        config = json.loads(zf.read("ccan.json"))
    assert "install.py" in names
    assert "wheels/prodeo-0.1.0-py3-none-any.whl" in names
    assert "wheels/prodeo_ccan-0.1.0-py3-none-any.whl" in names
    # Only the two first-party wheels are bundled.
    assert len([n for n in names if n.startswith("wheels/")]) == 2

    assert config["hub"]["node"] == "hub"
    assert "BEGIN CERTIFICATE" in config["hub"]["certificate_pem"]
    assert config["hub"]["address_hint"] == "https://hub.example"
    assert config["port"] == DEFAULT_CCAN_PORT
    # The baked token is a real minted enrollment.
    assert await enrollments.claim(config["enroll_token"], node="worker-01") is True


@pytest.mark.asyncio
async def test_each_download_gets_its_own_token(tmp_path: Path) -> None:
    _seed_wheels(tmp_path / "wheels")
    builder, _ = _builder(tmp_path, workspace=tmp_path)

    def _token(path: Path) -> str:
        with zipfile.ZipFile(path) as zf:
            token: str = json.loads(zf.read("ccan.json"))["enroll_token"]
            return token

    assert _token(await builder.build()) != _token(await builder.build())


@pytest.mark.asyncio
async def test_unavailable_without_a_workspace(tmp_path: Path) -> None:
    builder, _ = _builder(tmp_path, workspace=None)
    assert builder.unavailable_reason() is not None
    with pytest.raises(InstallerUnavailableError):
        await builder.build()
