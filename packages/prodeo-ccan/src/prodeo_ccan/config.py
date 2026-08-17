"""CCAN configuration: the ``ccan.json`` its installer wrote.

The file is the pairing: it carries the parent hub's certificate (this
node's whole trust store) and the enrollment token that proves to the hub
this install came from one of its own installers (ADR-0025).
"""

import json
import os
import platform
import sys
from pathlib import Path

from pydantic import BaseModel, Field

#: Mirrors ``prodeo.machines.pairing.DEFAULT_CCAN_PORT``; keep in sync.
DEFAULT_PORT = 8422


def default_data_dir() -> Path:
    """Where the node keeps its config and identity.

    Mirrored by the installer's ``install.py`` ``default_dir()``; keep the
    two in sync.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "prodeo-ccan"
    return Path.home() / ".local" / "share" / "prodeo-ccan"


class HubRef(BaseModel):
    """The parent Command Center, as baked into the installer."""

    node: str = ""
    certificate_pem: str
    #: Best-effort dashboard URL, for humans reading the file.
    address_hint: str = ""


class CcanConfig(BaseModel):
    hub: HubRef
    enroll_token: str
    port: int = DEFAULT_PORT
    node_name: str = Field(default_factory=platform.node)
    data_dir: Path = Field(default_factory=default_data_dir)
    #: Per-adapter config, keyed by adapter name — same shape as the hub's
    #: ``PRODEO_ADAPTERS``; flows into each adapter's ``AdapterContext``.
    adapters: dict[str, dict[str, object]] = Field(default_factory=dict)
    #: How often adapters re-scan for sessions (0 disables the loop).
    discovery_interval_s: float = 10.0

    @classmethod
    def load(cls, path: Path) -> "CcanConfig":
        """Parse a ``ccan.json``; raises with the path on any problem."""
        try:
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            raise FileNotFoundError(
                f"no CCAN config at {path} — run the installer downloaded from "
                "your Command Center first, or pass --config"
            ) from None
        except ValueError as exc:
            raise ValueError(f"invalid CCAN config at {path}: {exc}") from exc
