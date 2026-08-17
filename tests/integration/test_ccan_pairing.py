"""End-to-end CCAN pairing over real mutual TLS (ADR-0025).

Boots the CCAN daemon the way ``prodeo_ccan.main`` does — uvicorn, TLS with
the node's own certificate, and the parent hub's certificate as the entire
client trust store — then exercises the hub's real pairing client against
it. The point under test is the parent-only rule at the transport: the
parent's certificate pairs, a stranger without it is refused at the door,
and so is a *different hub* with a certificate of its own.
"""

import asyncio
import socket
import ssl
from pathlib import Path

import httpx
import pytest
import uvicorn

from prodeo.errors import PairingError
from prodeo.identity import IdentityProvider, ensure_identity
from prodeo.machines.enrollments import Enrollments
from prodeo.machines.pairing import CcanPairingClient
from prodeo_ccan.app import create_app
from prodeo_ccan.config import CcanConfig

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_until_started(server: uvicorn.Server) -> None:
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_pairing_over_mutual_tls_is_parent_only(tmp_path: Path) -> None:
    # The hub: identity plus one minted installer token.
    hub_identity = IdentityProvider(tmp_path / "hub", common_name="hub-01")
    hub = await hub_identity.get()
    enrollments = Enrollments(tmp_path / "hub" / "enrollments.json")
    token = await enrollments.mint(label="test-installer")

    # The node: config exactly as the installer writes it.
    config = CcanConfig.model_validate(
        {
            "hub": {"node": "hub-01", "certificate_pem": hub.certificate_pem},
            "enroll_token": token,
            "node_name": "worker-01",
            "data_dir": str(tmp_path / "ccan"),
        }
    )
    ccan_identity = ensure_identity(config.data_dir / "identity", common_name="worker-01")
    hub_ca = config.data_dir / "hub-ca.pem"
    hub_ca.write_text(config.hub.certificate_pem, encoding="utf-8")

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config, ccan_identity),
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(ccan_identity.cert_path),
            ssl_keyfile=str(ccan_identity.key_path),
            ssl_ca_certs=str(hub_ca),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
            log_level="warning",
        )
    )
    serve_task = asyncio.create_task(server.serve())
    try:
        await _wait_until_started(server)
        address = f"127.0.0.1:{port}"

        # A caller with no client certificate never reaches a handler.
        with pytest.raises(httpx.HTTPError):
            async with httpx.AsyncClient(verify=False, timeout=5) as nosy:
                await nosy.post(f"https://{address}/ccan/v1/pair", json={})

        # A *different* Command Center — real certificate, wrong parent —
        # is refused the same way and surfaces as a pairing failure.
        stranger = CcanPairingClient(
            IdentityProvider(tmp_path / "hub2", common_name="hub-02"),
            Enrollments(tmp_path / "hub2" / "enrollments.json"),
            hub_node="hub-02",
        )
        with pytest.raises(PairingError):
            await stranger.pair(address)

        # The parent pairs, and gets the node's identity to pin.
        client = CcanPairingClient(hub_identity, enrollments, hub_node="hub-01")
        paired = await client.pair(address)
        assert paired.node == "worker-01"
        assert paired.certificate_pem == ccan_identity.certificate_pem

        # Re-pairing the same machine keeps working (token bound, not burned).
        assert (await client.pair(address)).node == "worker-01"
    finally:
        server.should_exit = True
        await serve_task
