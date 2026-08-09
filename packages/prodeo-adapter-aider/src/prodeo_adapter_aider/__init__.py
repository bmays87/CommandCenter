"""Prodeo Command Center adapter for Aider."""

from prodeo.plugins import PluginManifest
from prodeo_adapter_aider.adapter import VERSION, AiderAdapter


def create_adapter() -> AiderAdapter:
    """Adapter factory (zero-arg; config arrives via the AdapterContext)."""
    return AiderAdapter()


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group)."""
    return PluginManifest(
        name="aider",
        kind="adapter",
        version=VERSION,
        factory=create_adapter,
        description="Observe Aider sessions. Observation only - no launch or control.",
        publisher="Prodeo",
        homepage="https://github.com/bmays87/CommandCenter/tree/main/packages/prodeo-adapter-aider",
        license="Apache-2.0",
        categories=["adapter"],
    )


__all__ = ["AiderAdapter", "create_adapter", "manifest"]
