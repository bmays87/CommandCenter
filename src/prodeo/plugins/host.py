"""The Plugin Host: discovers, validates, and instantiates plugins.

Plugins are Python packages exposing an entry point in the ``prodeo.plugins``
group (ADR-0005). The entry point resolves to a :class:`PluginManifest` (or a
zero-argument callable returning one) that declares the plugin's kind, its
API version, and an optional Pydantic config schema. The host:

- refuses API-version mismatches with a clear error, not a crash;
- validates user config against the declared schema *before* instantiation,
  so misconfiguration is reported at startup, not mid-flight;
- contains every failure as a ``system.plugin_failed`` event and keeps loading.

Factory signatures by kind (see docs/development/plugin-packaging.md):

- ``adapter`` — zero-argument; per-adapter config flows through the
  ``AdapterContext`` at ``start()`` as it always has.
- ``notifier`` / ``summarizer`` — called with the validated config
  (the ``config_model`` instance when declared, else the raw dict).

For backward compatibility an entry point may still resolve to a bare
zero-argument adapter factory (the Phase 1 contract); it is treated as an
``adapter`` manifest with no config schema.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Final, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict

from prodeo.adapters.interface import ADAPTER_API_VERSION, AgentAdapter
from prodeo.bus.interface import EventBus
from prodeo.events import new_event
from prodeo.events import types as ev
from prodeo.notify.interface import NotificationChannel
from prodeo.summary.interface import Summarizer

_log = structlog.get_logger(__name__)

PLUGIN_ENTRY_POINT_GROUP: Final = "prodeo.plugins"

#: Bumped when the manifest/host contract changes incompatibly.
PLUGIN_API_VERSION: Final = 1

PluginKind = Literal["adapter", "notifier", "summarizer"]


class PluginManifest(BaseModel):
    """What a plugin package declares about itself (see module docstring)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    kind: PluginKind
    version: str
    plugin_api_version: int = PLUGIN_API_VERSION
    #: Optional Pydantic schema; user config is validated against it before
    #: the factory runs.
    config_model: type[BaseModel] | None = None
    #: Zero-argument for ``adapter``; ``factory(config)`` otherwise.
    factory: Callable[..., Any]


@dataclass
class LoadedPlugins:
    """Everything the host produced, grouped by kind for the composition root."""

    adapters: list[AgentAdapter] = field(default_factory=list)
    channels: dict[str, NotificationChannel] = field(default_factory=dict)
    summarizers: dict[str, Summarizer] = field(default_factory=dict)


class _EntryPointLike(Protocol):
    """The slice of ``importlib.metadata.EntryPoint`` the host uses."""

    @property
    def name(self) -> str: ...

    def load(self) -> Any: ...


def _installed_entry_points() -> Iterable[_EntryPointLike]:
    return entry_points(group=PLUGIN_ENTRY_POINT_GROUP)


class PluginHost:
    """Loads every installed plugin; failures are contained, never fatal."""

    def __init__(
        self,
        bus: EventBus,
        *,
        node: str = "local",
        adapter_config: dict[str, dict[str, Any]] | None = None,
        channel_config: dict[str, dict[str, Any]] | None = None,
        plugin_config: dict[str, dict[str, Any]] | None = None,
        entry_points_fn: Callable[[], Iterable[_EntryPointLike]] = _installed_entry_points,
    ) -> None:
        self._bus = bus
        self._node = node
        self._config_by_kind: dict[str, dict[str, dict[str, Any]]] = {
            "adapter": adapter_config or {},
            "notifier": channel_config or {},
            "summarizer": plugin_config or {},
        }
        self._entry_points = entry_points_fn

    async def load(self) -> LoadedPlugins:
        """Resolve every ``prodeo.plugins`` entry point into live instances."""
        loaded = LoadedPlugins()
        for ep in self._entry_points():
            try:
                manifest = self._resolve_manifest(ep)
                self._instantiate(manifest, loaded)
            except Exception as exc:
                _log.exception("plugins.load_failed", entry_point=ep.name)
                await self._bus.publish(
                    new_event(
                        ev.SYSTEM_PLUGIN_FAILED,
                        node=self._node,
                        source="plugin-host",
                        payload={"plugin": ep.name, "error": str(exc)},
                    )
                )
                continue
            await self._bus.publish(
                new_event(
                    ev.SYSTEM_PLUGIN_LOADED,
                    node=self._node,
                    source="plugin-host",
                    payload={
                        "plugin": manifest.name,
                        "kind": manifest.kind,
                        "version": manifest.version,
                    },
                )
            )
        return loaded

    def _resolve_manifest(self, ep: _EntryPointLike) -> PluginManifest:
        obj = ep.load()
        if isinstance(obj, PluginManifest):
            manifest = obj
        else:
            produced = obj()
            if isinstance(produced, PluginManifest):
                manifest = produced
            elif isinstance(produced, AgentAdapter):
                # Legacy Phase 1 contract: a bare zero-arg adapter factory.
                return PluginManifest(
                    name=produced.metadata.name,
                    kind="adapter",
                    version=produced.metadata.version,
                    factory=lambda instance=produced: instance,
                )
            else:
                raise TypeError(
                    f"entry point {ep.name!r} must resolve to a PluginManifest "
                    f"(or a zero-arg adapter factory), got {type(produced).__name__}"
                )
        if manifest.plugin_api_version != PLUGIN_API_VERSION:
            raise RuntimeError(
                f"plugin API version mismatch: {manifest.name!r} declares "
                f"{manifest.plugin_api_version}, core provides {PLUGIN_API_VERSION}"
            )
        return manifest

    def _instantiate(self, manifest: PluginManifest, loaded: LoadedPlugins) -> None:
        raw = self._config_by_kind[manifest.kind].get(manifest.name, {})
        config: Any = raw
        if manifest.config_model is not None:
            config = manifest.config_model.model_validate(raw)

        if manifest.kind == "adapter":
            adapter = manifest.factory()
            if not isinstance(adapter, AgentAdapter):
                raise TypeError(f"adapter plugin {manifest.name!r} did not produce an AgentAdapter")
            declared = adapter.metadata.adapter_api_version
            if declared != ADAPTER_API_VERSION:
                raise RuntimeError(
                    f"adapter API version mismatch: {manifest.name!r} declares "
                    f"{declared}, core provides {ADAPTER_API_VERSION}"
                )
            loaded.adapters.append(adapter)
        elif manifest.kind == "notifier":
            channel = manifest.factory(config)
            if not isinstance(channel, NotificationChannel):
                raise TypeError(
                    f"notifier plugin {manifest.name!r} did not produce a NotificationChannel"
                )
            loaded.channels[manifest.name] = channel
        else:
            summarizer = manifest.factory(config)
            if not isinstance(summarizer, Summarizer):
                raise TypeError(f"summarizer plugin {manifest.name!r} did not produce a Summarizer")
            loaded.summarizers[manifest.name] = summarizer
