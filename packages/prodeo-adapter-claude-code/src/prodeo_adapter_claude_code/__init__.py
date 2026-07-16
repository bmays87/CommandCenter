"""Prodeo Command Center adapter for Claude Code."""

from prodeo.plugins import PluginManifest
from prodeo_adapter_claude_code.adapter import VERSION, ClaudeCodeAdapter


def create_adapter() -> ClaudeCodeAdapter:
    """Adapter factory (zero-arg; config arrives via the AdapterContext)."""
    return ClaudeCodeAdapter()


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group): what this plugin is."""
    return PluginManifest(
        name="claude-code", kind="adapter", version=VERSION, factory=create_adapter
    )


__all__ = ["ClaudeCodeAdapter", "create_adapter", "manifest"]
