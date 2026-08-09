"""The sanctioned-extension catalog.

What a user can browse and (from milestone 2) install. This milestone ships a
bundled JSON file so the UI has real content and the response shape is pinned;
whether the production index is a reviewed file in a git repo or PyPI filtered
by the ``prodeo-<kind>-*`` naming convention is still open (ADR-0014).

The catalog is deliberately *not* the inventory: it describes what exists,
:mod:`prodeo.extensions.service` describes what is installed. The UI joins them
by name.
"""

import json
from pathlib import Path
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field

_log = structlog.get_logger(__name__)

#: Ships with the package so a fresh install has a catalog with no network.
BUNDLED_CATALOG = Path(__file__).with_name("catalog.json")


class CatalogEntry(BaseModel):
    """One extension offered by the index."""

    name: str
    #: ``plugin`` runs in-process; ``app`` is a separate process (ADR-0014).
    extension_class: str = "plugin"
    kind: str = ""
    version: str = ""
    description: str = ""
    publisher: str = ""
    homepage: str = ""
    license: str = ""
    categories: list[str] = Field(default_factory=list)
    #: The distribution to install, e.g. ``prodeo-summarizer-ollama``.
    package: str = ""


class Catalog(BaseModel):
    source: str
    entries: list[CatalogEntry] = Field(default_factory=list)


class ExtensionCatalog(Protocol):
    """Where the list of sanctioned extensions comes from."""

    async def fetch(self) -> Catalog: ...


class BundledCatalog:
    """Reads the catalog shipped inside the package."""

    def __init__(self, path: Path = BUNDLED_CATALOG) -> None:
        self._path = path

    async def fetch(self) -> Catalog:
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A missing or malformed catalog is a degraded browse experience,
            # never a reason to fail the request.
            _log.exception("extensions.catalog_unreadable", path=str(self._path))
            return Catalog(source="bundled", entries=[])
        return Catalog(source="bundled", entries=[CatalogEntry.model_validate(e) for e in raw])
