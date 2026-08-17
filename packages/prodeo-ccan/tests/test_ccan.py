"""CCAN config parsing and the pairing/health surface.

The mutual-TLS transport gate lives in main() (uvicorn ssl options); the
end-to-end proof that only the parent's certificate passes is
tests/integration/test_ccan_pairing.py in the workspace root.
"""

import json
from pathlib import Path

import httpx
import pytest

from prodeo.identity import ensure_identity
from prodeo_ccan import __version__
from prodeo_ccan.app import create_app
from prodeo_ccan.config import DEFAULT_PORT, CcanConfig


def _write_config(path: Path, **overrides: object) -> Path:
    doc: dict[str, object] = {
        "hub": {"node": "hub-01", "certificate_pem": "PEM", "address_hint": ""},
        "enroll_token": "tok",
        **overrides,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_config_loads_with_defaults(tmp_path: Path) -> None:
    config = CcanConfig.load(_write_config(tmp_path / "ccan.json"))
    assert config.hub.node == "hub-01"
    assert config.enroll_token == "tok"
    assert config.port == DEFAULT_PORT
    assert config.node_name  # the machine's hostname


def test_missing_config_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"ccan\.json"):
        CcanConfig.load(tmp_path / "ccan.json")


@pytest.mark.asyncio
async def test_pair_answers_with_identity_and_token(tmp_path: Path) -> None:
    config = CcanConfig.load(_write_config(tmp_path / "ccan.json", node_name="worker-01"))
    identity = ensure_identity(tmp_path / "identity", common_name="worker-01")
    app = create_app(config, identity)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://ccan") as client:
        pair = (await client.post("/ccan/v1/pair", json={"hub_node": "hub-01"})).json()
        health = (await client.get("/ccan/v1/health")).json()

    assert pair["node"] == "worker-01"
    assert pair["enroll_token"] == "tok"
    assert pair["certificate_pem"] == identity.certificate_pem
    assert pair["version"] == __version__
    assert health == {"status": "ok", "node": "worker-01", "version": __version__}
