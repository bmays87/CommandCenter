"""Node identity: a self-signed certificate minted once and kept forever.

Both sides of the CCAN split use this (ADR-0025). The hub's certificate is
what installers bake into every CCAN so the node can recognize its parent;
each CCAN mints its own the same way for its TLS listener. The certificate
doubles as a TLS *client* certificate when the hub dials a CCAN — the CCAN
requires it at the handshake, which is what enforces the parent-only rule at
the transport layer.

Deliberately boring crypto: one EC P-256 key, one self-signed certificate
valid for ten years, both PEM files in a directory. Rotation is re-pairing
(documented in ADR-0025); there is no chain to manage.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_log = structlog.get_logger(__name__)

_VALIDITY = timedelta(days=3650)

CERT_FILE = "identity.pem"
KEY_FILE = "identity.key"


@dataclass(frozen=True)
class Identity:
    """One node's key material, loaded or freshly minted."""

    cert_path: Path
    key_path: Path
    certificate_pem: str
    #: SHA-256 of the DER certificate, hex — the stable short name for logs.
    fingerprint: str


def ensure_identity(directory: Path, *, common_name: str) -> Identity:
    """Load the identity in ``directory``, minting it on first call.

    Blocking file IO and key generation — call via ``asyncio.to_thread``
    from the event loop.
    """
    cert_path = directory / CERT_FILE
    key_path = directory / KEY_FILE
    if cert_path.is_file() and key_path.is_file():
        pem = cert_path.read_text(encoding="utf-8")
        return Identity(
            cert_path=cert_path,
            key_path=key_path,
            certificate_pem=pem,
            fingerprint=fingerprint(pem),
        )

    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _VALIDITY)
        # Its own trust anchor: verifiers load this one certificate as the
        # whole store, so it must be allowed to vouch for itself.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )

    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_path.write_text(pem, encoding="utf-8")
    key_path.write_bytes(key_pem)
    try:
        key_path.chmod(0o600)  # best effort; no-op semantics on Windows ACLs
    except OSError:  # pragma: no cover - platform-dependent
        _log.warning("identity.key_chmod_failed", path=str(key_path))
    identity = Identity(
        cert_path=cert_path,
        key_path=key_path,
        certificate_pem=pem,
        fingerprint=fingerprint(pem),
    )
    _log.info(
        "identity.minted",
        common_name=common_name,
        fingerprint=identity.fingerprint,
        path=str(directory),
    )
    return identity


def fingerprint(certificate_pem: str) -> str:
    """SHA-256 fingerprint (hex) of a PEM certificate."""
    cert = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
    return cert.fingerprint(hashes.SHA256()).hex()


class IdentityProvider:
    """Lazy async access to one node's identity, minted off the event loop.

    Construction is free (the composition root builds it before the loop has
    anything to block); the first :meth:`get` does the IO/keygen in a thread
    and every later call returns the cached identity.
    """

    def __init__(self, directory: Path, *, common_name: str) -> None:
        self._directory = directory
        self._common_name = common_name
        self._identity: Identity | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Identity:
        async with self._lock:
            if self._identity is None:
                self._identity = await asyncio.to_thread(
                    ensure_identity, self._directory, common_name=self._common_name
                )
            return self._identity
