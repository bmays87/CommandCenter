"""Prodeo Command Center adapter for OpenAI Codex CLI."""

from prodeo.plugins import PluginManifest
from prodeo_adapter_codex.adapter import VERSION, CodexAdapter


def create_adapter() -> CodexAdapter:
    """Adapter factory (zero-arg; config arrives via the AdapterContext)."""
    return CodexAdapter()


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group)."""
    return PluginManifest(
        name="codex",
        kind="adapter",
        version=VERSION,
        factory=create_adapter,
        description="Observe Codex CLI sessions. Observation only - no launch or control.",
        publisher="Prodeo",
        homepage="https://github.com/bmays87/CommandCenter/tree/main/packages/prodeo-adapter-codex",
        license="Apache-2.0",
        categories=["adapter"],
    )


__all__ = ["CodexAdapter", "create_adapter", "manifest"]
