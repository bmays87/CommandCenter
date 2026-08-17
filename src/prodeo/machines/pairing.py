"""The hub side of the Add Machine handshake (ADR-0025).

The transport enforces the parent-only rule: the CCAN's listener demands a
TLS client certificate and trusts exactly one — the hub certificate its
installer carried. This client presents it on every call, so a successful
response *is* proof the answerer holds a CCAN paired to this hub's identity.
The enrollment token the CCAN returns then proves the reverse — that its
installer came from this hub — closing the loop in both directions.

The CCAN's own certificate is not verifiable a priori (self-signed, first
contact), so pairing records it from the response for later pinning instead
of failing the handshake: trust-on-first-use, made safe by the two proofs
above. Workstream C pins subsequent calls against the recorded certificate.
"""

import ssl
from typing import Protocol

import httpx
import structlog
from pydantic import BaseModel

from prodeo.errors import PairingError
from prodeo.identity import IdentityProvider
from prodeo.machines.enrollments import Enrollments

_log = structlog.get_logger(__name__)

#: The port a CCAN listens on unless its config says otherwise; mirrored by
#: ``prodeo_ccan.config``. An explicit ``host:port`` address overrides it.
DEFAULT_CCAN_PORT = 8422


class PairedCcan(BaseModel):
    """What a successful handshake tells the hub about the node."""

    node: str
    name: str = ""
    version: str = ""
    certificate_pem: str = ""


class _PairResponse(BaseModel):
    """Wire shape of ``POST /ccan/v1/pair`` (mirrored by ``prodeo_ccan.app``)."""

    node: str
    name: str = ""
    version: str = ""
    certificate_pem: str = ""
    enroll_token: str


class NodePairing(Protocol):
    """What the API needs from pairing; faked in tests, real in server.py."""

    async def pair(self, address: str) -> PairedCcan:
        """Handshake with the CCAN at ``address`` (FQDN/IP, optional :port).

        Raises :class:`PairingError` with a user-facing message when no CCAN
        answers, the answer is malformed, or the enrollment token is not one
        this hub minted.
        """
        ...


class CcanPairingClient:
    """The real handshake: mutual TLS out, enrollment token verified back."""

    def __init__(
        self,
        identity: IdentityProvider,
        enrollments: Enrollments,
        *,
        hub_node: str,
        default_port: int = DEFAULT_CCAN_PORT,
        timeout_s: float = 10.0,
    ) -> None:
        self._identity = identity
        self._enrollments = enrollments
        self._hub_node = hub_node
        self._default_port = default_port
        self._timeout_s = timeout_s

    async def pair(self, address: str) -> PairedCcan:
        host, port = _split_address(address, self._default_port)
        url = f"https://{host}:{port}/ccan/v1/pair"
        identity = await self._identity.get()
        # Present the hub certificate as the TLS client certificate; skip
        # verifying the listener's. First contact with a self-signed cert:
        # authenticity comes from the client-cert gate + token, not this
        # check (module docstring); the response cert is recorded to pin.
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.load_cert_chain(certfile=identity.cert_path, keyfile=identity.key_path)
        try:
            async with httpx.AsyncClient(verify=context, timeout=self._timeout_s) as client:
                resp = await client.post(url, json={"hub_node": self._hub_node})
        except httpx.HTTPError as exc:
            raise PairingError(
                f"no CCAN answered at {host}:{port} — check the address, that "
                f"prodeo-ccan is running there, and that its installer came from "
                f"this Command Center ({exc})"
            ) from exc
        if resp.status_code != 200:
            raise PairingError(
                f"the CCAN at {host}:{port} refused pairing (HTTP {resp.status_code})"
            )
        try:
            body = _PairResponse.model_validate(resp.json())
        except ValueError as exc:
            raise PairingError(f"the service at {host}:{port} did not answer like a CCAN") from exc
        if not await self._enrollments.claim(body.enroll_token, node=body.node):
            raise PairingError(
                f"the CCAN at {host}:{port} presented an enrollment token this "
                "Command Center did not mint (or one already bound to another "
                "machine) — reinstall it from an installer downloaded here"
            )
        _log.info("pairing.succeeded", node=body.node, address=f"{host}:{port}")
        return PairedCcan(
            node=body.node,
            name=body.name,
            version=body.version,
            certificate_pem=body.certificate_pem,
        )


def _split_address(address: str, default_port: int) -> tuple[str, int]:
    host, sep, maybe_port = address.strip().rpartition(":")
    if sep and maybe_port.isdigit():
        return host, int(maybe_port)
    return address.strip(), default_port
