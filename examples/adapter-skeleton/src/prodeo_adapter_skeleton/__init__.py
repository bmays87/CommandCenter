"""Skeleton adapter: the smallest honest Prodeo adapter. Copy me.

See adapter.py for the walkthrough and docs/development/plugin-packaging.md
for the packaging story.
"""

from prodeo.plugins import PluginManifest
from prodeo_adapter_skeleton.adapter import SkeletonAdapter

VERSION = "0.1.0"


def create_adapter() -> SkeletonAdapter:
    """Adapter factories take no arguments: per-adapter config arrives later,
    through the AdapterContext at ``start()`` (``PRODEO_ADAPTERS`` JSON)."""
    return SkeletonAdapter()


def manifest() -> PluginManifest:
    """The entry point target: tells the Plugin Host what this package is."""
    return PluginManifest(name="skeleton", kind="adapter", version=VERSION, factory=create_adapter)


__all__ = ["SkeletonAdapter", "create_adapter", "manifest"]
