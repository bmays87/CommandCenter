"""The Plugin Host: entry-point discovery for every plugin kind (ADR-0005)."""

from prodeo.plugins.host import (
    PLUGIN_API_VERSION,
    PLUGIN_ENTRY_POINT_GROUP,
    VOICE_KINDS,
    ExtensionInfo,
    ExtensionStatus,
    LoadedPlugins,
    PluginHost,
    PluginKind,
    PluginManifest,
    resolve_manifest,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "PLUGIN_ENTRY_POINT_GROUP",
    "VOICE_KINDS",
    "ExtensionInfo",
    "ExtensionStatus",
    "LoadedPlugins",
    "PluginHost",
    "PluginKind",
    "PluginManifest",
    "resolve_manifest",
]
