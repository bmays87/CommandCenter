"""Identity minting: created once, stable across loads, self-anchored."""

from pathlib import Path

from cryptography import x509

from prodeo.identity import ensure_identity, fingerprint


def test_mints_once_and_reloads_the_same_identity(tmp_path: Path) -> None:
    first = ensure_identity(tmp_path / "identity", common_name="hub-01")
    second = ensure_identity(tmp_path / "identity", common_name="ignored-on-load")

    assert first.certificate_pem == second.certificate_pem
    assert first.fingerprint == second.fingerprint
    assert first.cert_path.is_file()
    assert first.key_path.is_file()


def test_certificate_is_self_signed_for_the_common_name(tmp_path: Path) -> None:
    identity = ensure_identity(tmp_path, common_name="hub-01")

    cert = x509.load_pem_x509_certificate(identity.certificate_pem.encode("ascii"))
    assert cert.subject == cert.issuer
    assert cert.subject.rfc4514_string() == "CN=hub-01"
    # Self-anchored: allowed to be its own one-cert trust store (ADR-0025).
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert constraints.ca is True
    assert fingerprint(identity.certificate_pem) == identity.fingerprint


def test_distinct_directories_get_distinct_keys(tmp_path: Path) -> None:
    a = ensure_identity(tmp_path / "a", common_name="node")
    b = ensure_identity(tmp_path / "b", common_name="node")
    assert a.fingerprint != b.fingerprint
