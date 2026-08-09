"""The extensions manager: what is installed, how it is configured (ADR-0014).

A presentation and configuration layer over the Plugin Host - it does not load
or run anything itself. The Plugin Host stays the single place plugins are
discovered and instantiated; this package answers "what did it find, what is
each one configured with, and what can the user change".
"""

from prodeo.extensions.catalog import (
    BundledCatalog,
    Catalog,
    CatalogEntry,
    ExtensionCatalog,
)
from prodeo.extensions.service import (
    ExtensionConfig,
    ExtensionDetail,
    ExtensionService,
    ExtensionSummary,
)
from prodeo.extensions.store import (
    ConfigMap,
    ExtensionConfigStore,
    JsonFileConfigStore,
)

__all__ = [
    "BundledCatalog",
    "Catalog",
    "CatalogEntry",
    "ConfigMap",
    "ExtensionCatalog",
    "ExtensionConfig",
    "ExtensionConfigStore",
    "ExtensionDetail",
    "ExtensionService",
    "ExtensionSummary",
    "JsonFileConfigStore",
]
