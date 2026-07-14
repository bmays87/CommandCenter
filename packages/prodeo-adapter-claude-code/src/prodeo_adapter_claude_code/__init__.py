"""Prodeo Command Center adapter for Claude Code."""

from prodeo_adapter_claude_code.adapter import ClaudeCodeAdapter


def create_adapter() -> ClaudeCodeAdapter:
    """Entry point factory (``prodeo.plugins`` group)."""
    return ClaudeCodeAdapter()


__all__ = ["ClaudeCodeAdapter", "create_adapter"]
