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
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, Field

from prodeo.environment import Environment

_log = structlog.get_logger(__name__)

#: Ships with the package so a fresh install has a catalog with no network.
BUNDLED_CATALOG = Path(__file__).with_name("catalog.json")


#: What it takes to obtain an extension.
#:
#: ``bundled`` ships with Command Center and is installed by the normal
#: workspace sync - offering an "Install" button for it would only ever fail.
#: ``free`` is publicly installable. ``paid`` requires an entitlement; the
#: check is a placeholder today (see :mod:`prodeo.extensions.service`) but the
#: tier is declared now so the catalog, API, and UI already carry it.
ExtensionTier = Literal["bundled", "free", "paid"]


class ExtensionRequirements(BaseModel):
    """What a machine must provide before an extension is worth installing.

    Checked against :class:`prodeo.environment.Environment` before anything is
    downloaded - Parakeet pulls ~250MB before it would otherwise discover there
    is no GPU.
    """

    #: Needs an NVIDIA GPU. Note this is about the *device*; CUDA being
    #: installed is a separate prerequisite and belongs in ``note``.
    gpu: bool = False
    #: Interpreter floor, ``"3.10"``-style. Empty = any.
    min_python: str = ""
    #: ``sys.platform`` values this runs on. Empty = any.
    platforms: list[str] = Field(default_factory=list)
    #: What the user should understand before committing to the install.
    note: str = ""


class ExtensionAsset(BaseModel):
    """A model file an extension needs, and how to fetch it.

    Declared as data so core can provision it without knowing what the engine
    is. ``{python}`` and ``{models_dir}`` are the only substitutions.
    """

    id: str
    label: str = ""
    approx_mb: int = 0
    #: argv to run. Never a shell string.
    command: list[str] = Field(default_factory=list)
    #: Directory to create before running, if the downloader will not.
    into: str = ""
    #: Every file that must exist afterwards. Checking *all* of them is what
    #: catches a voice model downloaded without its ``.onnx.json`` sibling.
    produces: list[str] = Field(default_factory=list)
    #: App whose saved config receives the downloaded path...
    config_app: str = ""
    #: ...at this dotted pointer, e.g. ``engines.piper.voice_path``.
    config_pointer: str = ""


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
    tier: ExtensionTier = "free"
    #: Shown when a paid extension is not entitled - what the user gets and
    #: how to obtain it. Never a price; that belongs to the storefront.
    tier_note: str = ""
    requires: ExtensionRequirements = Field(default_factory=ExtensionRequirements)
    #: Model files to fetch before this extension can work.
    assets: list[ExtensionAsset] = Field(default_factory=list)
    #: Config applied when a models directory is chosen, so one root decision
    #: reaches every engine that has a cache knob. ``setdefault`` semantics:
    #: an explicit value the user set is never overwritten.
    config_defaults: dict[str, str] = Field(default_factory=dict)
    #: Computed by the server, never read from the catalog file: the
    #: requirements this host does *not* satisfy. Empty = installable here.
    unmet: list[str] = Field(default_factory=list)


class Catalog(BaseModel):
    source: str
    entries: list[CatalogEntry] = Field(default_factory=list)


def unmet_requirements(entry: CatalogEntry, env: Environment) -> list[str]:
    """Which of ``entry``'s requirements this host fails, in plain words."""
    reasons: list[str] = []
    if entry.requires.gpu and not env.nvidia_gpu:
        reasons.append("needs an NVIDIA GPU; none detected")
    if entry.requires.min_python and not env.python_at_least(entry.requires.min_python):
        running = ".".join(str(part) for part in env.python)
        reasons.append(f"needs Python {entry.requires.min_python}+; running {running}")
    if entry.requires.platforms and env.platform not in entry.requires.platforms:
        supported = ", ".join(entry.requires.platforms)
        reasons.append(f"runs on {supported}; this is {env.platform}")
    return reasons


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
