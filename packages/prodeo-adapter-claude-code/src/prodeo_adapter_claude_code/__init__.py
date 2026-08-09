"""Prodeo Command Center adapter for Claude Code."""

from prodeo.plugins import PluginManifest
from prodeo_adapter_claude_code.adapter import VERSION, ClaudeCodeAdapter


def create_adapter() -> ClaudeCodeAdapter:
    """Adapter factory (zero-arg; config arrives via the AdapterContext)."""
    return ClaudeCodeAdapter()


def manifest() -> PluginManifest:
    """Entry point (``prodeo.plugins`` group): what this plugin is."""
    return PluginManifest(
        name="claude-code",
        kind="adapter",
        version=VERSION,
        factory=create_adapter,
        description=(
            "Supervise Claude Code sessions: live and historical observation, "
            "plus launch, terminate, and prompt via the Agent SDK."
        ),
        publisher="Prodeo",
        homepage="https://github.com/bmays87/CommandCenter/tree/main/packages/prodeo-adapter-claude-code",
        license="Apache-2.0",
        categories=["adapter"],
    )


__all__ = ["ClaudeCodeAdapter", "create_adapter", "manifest"]
