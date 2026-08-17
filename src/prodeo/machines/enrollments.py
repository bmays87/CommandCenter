"""Enrollment tokens: how the hub knows a pairing CCAN is one of its own.

A token is minted when an installer is downloaded and baked into that
installer's config. At pairing the CCAN presents it back over the
client-cert-authenticated channel, and the hub accepts only tokens it minted
(ADR-0025). Only SHA-256 hashes are stored — the raw token exists in the
installer artifact and nowhere else on the hub — so this file is not a
secret store, but it lives beside the private key anyway.

A token binds to the first node that claims it: re-pairing the same machine
(remove, then add again) keeps working, while a token lifted from one
machine cannot enroll a second one.
"""

import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

_log = structlog.get_logger(__name__)

STORE_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


class Enrollment(BaseModel):
    """One minted installer token (hash only)."""

    label: str = ""
    created_at: datetime = Field(default_factory=_now)
    #: Node identity of the first claimant; empty until first pairing.
    claimed_by: str = ""


class EnrollmentState(BaseModel):
    """The whole persisted document."""

    version: int = STORE_VERSION
    #: SHA-256 hex of the raw token -> its record.
    tokens: dict[str, Enrollment] = Field(default_factory=dict)


class Enrollments:
    """JSON-file-backed token registry (single process, atomic replace)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: EnrollmentState | None = None

    async def mint(self, *, label: str = "") -> str:
        """Create a token and return it — the only time it exists raw."""
        token = secrets.token_urlsafe(32)
        state = self._read()
        state.tokens[_digest(token)] = Enrollment(label=label)
        self._write(state)
        _log.info("enrollments.minted", label=label)
        return token

    async def claim(self, token: str, *, node: str) -> bool:
        """Accept ``token`` for ``node``: unknown or already-owned = False."""
        state = self._read()
        record = state.tokens.get(_digest(token))
        if record is None:
            return False
        if record.claimed_by not in ("", node):
            _log.warning("enrollments.claim_conflict", node=node, holder=record.claimed_by)
            return False
        if record.claimed_by != node:
            record.claimed_by = node
            self._write(state)
        return True

    async def state(self) -> EnrollmentState:
        return self._read().model_copy(deep=True)

    def _read(self) -> EnrollmentState:
        if self._cache is not None:
            return self._cache
        if not self._path.is_file():
            self._cache = EnrollmentState()
            return self._cache
        try:
            self._cache = EnrollmentState.model_validate(
                json.loads(self._path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            # Boot must not fail on a bad file; minted tokens are re-mintable.
            _log.exception("enrollments.unreadable", path=str(self._path))
            self._cache = EnrollmentState()
        return self._cache

    def _write(self, state: EnrollmentState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._path)
        self._cache = state


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
