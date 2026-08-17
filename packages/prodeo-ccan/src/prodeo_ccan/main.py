"""CCAN entry point: load the pairing config, mint identity, listen.

The listener is mutual TLS with a one-certificate trust store: it presents
this node's own self-signed identity and *requires* a client certificate
chaining to the parent hub's certificate — which is self-anchored, so the
parent's exact key and no other passes (ADR-0025). Everything above the
transport can therefore trust its caller unconditionally.
"""

import argparse
import ssl
from pathlib import Path

import uvicorn

from prodeo.identity import ensure_identity
from prodeo_ccan.app import create_app
from prodeo_ccan.config import CcanConfig, default_data_dir
from prodeo_ccan.node import NodeHost


def main() -> None:
    parser = argparse.ArgumentParser(description="Prodeo Command Center Agent Node")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_data_dir() / "ccan.json",
        help="path to ccan.json (default: %(default)s)",
    )
    args = parser.parse_args()

    config = CcanConfig.load(args.config)
    identity = ensure_identity(config.data_dir / "identity", common_name=config.node_name)
    # uvicorn wants the trust store as a file; materialize the packaged cert.
    hub_ca = config.data_dir / "hub-ca.pem"
    hub_ca.parent.mkdir(parents=True, exist_ok=True)
    hub_ca.write_text(config.hub.certificate_pem, encoding="utf-8")

    print(f"prodeo-ccan: node {config.node_name!r}, parent hub {config.hub.node!r}")
    print(f"listening on 0.0.0.0:{config.port} (mutual TLS, parent-only)")
    uvicorn.run(
        create_app(config, identity, NodeHost(config)),
        # All interfaces on purpose: the hub dials in from another machine.
        # Only the parent passes the client-certificate requirement below.
        host="0.0.0.0",
        port=config.port,
        ssl_certfile=str(identity.cert_path),
        ssl_keyfile=str(identity.key_path),
        ssl_ca_certs=str(hub_ca),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        log_level="info",
    )


if __name__ == "__main__":
    main()
