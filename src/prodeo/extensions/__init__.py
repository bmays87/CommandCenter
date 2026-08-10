"""The extensions manager: what is installed, configured, and enabled.

A presentation, configuration, and installation layer over the Plugin Host
(ADR-0014, ADR-0015) - it does not load or run plugins itself. The Plugin Host
stays the single place plugins are discovered and instantiated; this package
answers "what did it find, how is each one configured, is it turned on, and
what else could the user install".
"""

from prodeo.extensions.assets import AppSetupGap, AssetProvisioner, AssetResult, AssetStatus
from prodeo.extensions.catalog import (
    BundledCatalog,
    Catalog,
    CatalogEntry,
    ExtensionAsset,
    ExtensionCatalog,
    ExtensionRequirements,
    ExtensionTier,
    unmet_requirements,
)
from prodeo.extensions.installer import (
    InstallResult,
    PackageInstaller,
    TargetDirInstaller,
)
from prodeo.extensions.paths import (
    activate_extension_path,
    extension_lib_dir,
    local_index_dir,
    workspace_root,
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
    ExtensionSettings,
    ExtensionState,
    JsonFileConfigStore,
)

__all__ = [
    "AppSetupGap",
    "AssetProvisioner",
    "AssetResult",
    "AssetStatus",
    "BundledCatalog",
    "Catalog",
    "CatalogEntry",
    "ConfigMap",
    "ExtensionAsset",
    "ExtensionCatalog",
    "ExtensionConfig",
    "ExtensionConfigStore",
    "ExtensionDetail",
    "ExtensionRequirements",
    "ExtensionService",
    "ExtensionSettings",
    "ExtensionState",
    "ExtensionSummary",
    "ExtensionTier",
    "InstallResult",
    "JsonFileConfigStore",
    "PackageInstaller",
    "TargetDirInstaller",
    "activate_extension_path",
    "extension_lib_dir",
    "local_index_dir",
    "unmet_requirements",
    "workspace_root",
]
