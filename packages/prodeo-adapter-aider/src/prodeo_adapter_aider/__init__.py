"""Prodeo Command Center adapter for Aider."""

from prodeo.plugins import PluginManifest
from prodeo_adapter_aider.adapter import VERSION, AiderAdapter


def create_adapter() -> AiderAdapter:
    """Adapter factory (zero-arg; config arrives via the AdapterContext)."""
    return AiderAdapter()


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group)."""
    return PluginManifest(name="aider", kind="adapter", version=VERSION, factory=create_adapter)


__all__ = ["AiderAdapter", "create_adapter", "manifest"]
