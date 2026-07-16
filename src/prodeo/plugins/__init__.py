"""The Plugin Host: entry-point discovery for every plugin kind (ADR-0005)."""

from prodeo.plugins.host import (
    PLUGIN_API_VERSION,
    PLUGIN_ENTRY_POINT_GROUP,
    LoadedPlugins,
    PluginHost,
    PluginManifest,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "PLUGIN_ENTRY_POINT_GROUP",
    "LoadedPlugins",
    "PluginHost",
    "PluginManifest",
]
