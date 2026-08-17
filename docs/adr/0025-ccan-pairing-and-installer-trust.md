# ADR-0025: CCAN pairing, node identity, and installer trust

- **Status**: Accepted
- **Date**: 2026-08-16
- **Extends**: ADR-0020 (the CCAN split), ADR-0024 (machine registry);
  Phase 6 plan §3–4 (installer distribution, parent-only trust)

## Context

Phase 6 workstream B makes machines actually joinable: the hub must produce
CCAN installers from its UI, and a CCAN must answer **only** the Command
Center that spawned it — no connection is trusted unless it originates from
the parent (the product's non-negotiable rule). That needs an identity for
the hub, a way to bake it into installers, a listener on the node that
enforces it, and a handshake that also proves the *node* to the *hub*.

## Decision

### 1. Identity: one self-signed certificate per node, minted at first boot

`prodeo.identity` mints an EC P-256 key + self-signed certificate (10-year
validity, CN = node name, self-anchored with `CA:TRUE, pathlen:0`) into
`<data_dir>/identity/` on first boot, for hub and CCAN alike. No CA, no
chain, no user-supplied certificates in v1: the certificate *is* the
identity, and verifying a peer means "is this exactly my parent's key".
**Rotation is re-pairing**: a hub with a new certificate produces new
installers, and existing CCANs must be reinstalled from one. Documented
limitation, revisit if rotation becomes a real operation.

### 2. The parent-only rule is enforced at the TLS layer

The CCAN's listener (uvicorn, `prodeo_ccan.main`) serves TLS with its own
certificate and **requires a client certificate**, with the packaged hub
certificate as its *entire* trust store. Because that certificate is
self-anchored, exactly the parent's key passes the handshake — a different
Command Center with a perfectly valid certificate of its own is refused at
the door (proven end-to-end in `tests/integration/test_ccan_pairing.py`).
Handlers therefore contain zero auth logic; anything that reaches one is
the parent.

### 3. The handshake proves both directions

`POST /ccan/v1/pair` (hub → CCAN, client-cert presented): the CCAN answers
with its node identity, version, its own certificate, and the **enrollment
token** its installer carried. The hub accepts only tokens it minted
(`Enrollments`, SHA-256 hashes in `<data_dir>/identity/enrollments.json` —
the raw token exists only inside installer artifacts). A token **binds to
the first node that claims it**: re-pairing the same machine keeps working;
a token lifted onto a second machine does not enroll it.

The hub does not verify the CCAN's TLS certificate on first contact (it is
self-signed and unknown) — trust-on-first-use, made safe by the two proofs
above: only the real parent can elicit an answer at all (client-cert gate),
and only a real child of this hub can present a minted token. The answered
certificate is recorded on the `Machine` (`Machine.certificate`, additive)
for workstream C to pin every subsequent call against.

### 4. Installers are minted per download, by the hub, platform-agnostic

`GET /api/ccan/installers/any/download` (write-gated: the artifact is a
credential) builds a zip: a stdlib-only `install.py`, `ccan.json` (hub
certificate + fresh enrollment token + port), and the two unpublished
first-party wheels (`prodeo`, `prodeo-ccan`) built with the same
`uv build --all-packages` path and wheel cache the extensions manager uses
(`<data_dir>/extensions/local-index`). Third-party dependencies resolve
from PyPI on the target, keeping the artifact small and the installer
runnable on anything with Python 3.12+ — one artifact, all platforms,
satisfying the plan's platform-agnostic goal. A hub not running from a
source checkout reports *why* it cannot produce installers instead of
hiding the button.

### 5. `prodeo-ccan` is a workspace package that depends on `prodeo`

The node daemon reuses core code (identity, models) rather than duplicating
it, and workstream C will need core adapter machinery on the node anyway.
Default port **8422** (`DEFAULT_CCAN_PORT`), listener on all interfaces —
the client-certificate requirement is the access control.

## Consequences

- Add Machine works end to end: download installer → run on target →
  Add Machine by FQDN/IP → tab appears. The hub dials the CCAN for
  pairing; whether routine traffic stays hub→node or flips to a
  persistent outbound channel remains workstream C's transport ADR.
- The enrollment file and private key make `<data_dir>/identity/` the one
  directory that must never leave the machine or enter the event log.
- A stolen installer zip is the credential to enroll **one** machine as
  this hub's child (until its token is bound). Treat installers like
  credentials; minting is token-gated and each download gets its own.

## Alternatives Considered

- **User-supplied / CA-issued certificates.** Deferred: real value only
  appears with rotation and multi-hub stories; the self-anchored cert
  answers "is this my parent" with no PKI to operate.
- **Shared static secret instead of per-download tokens.** Rejected: one
  leak enrolls unlimited machines, and revocation would revoke everyone.
- **Hub verifies the CCAN's TLS cert at pairing (no TOFU).** Rejected for
  v1: there is nothing to verify against before first contact short of
  having the installer phone home first, which inverts the "Add Machine
  takes an address" flow the product specifies. The token closes the gap.
- **Bundling every third-party wheel per platform.** Rejected: multiplies
  artifacts per OS/arch, defeating the platform-agnostic goal; the target
  machine already needs network to reach the hub.
