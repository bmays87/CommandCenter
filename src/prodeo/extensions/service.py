"""Read/write view of installed extensions for the API layer.

Joins three things the extensions manager needs and nothing else has together:
what the Plugin Host discovered (:class:`~prodeo.plugins.ExtensionInfo`), the
environment config layer the server booted with, and the persisted overlay in
:mod:`prodeo.extensions.store`.

Config precedence is environment first, saved overlay on top (ADR-0014).
Validation always runs against the *merged* result, never the overlay alone -
editing one field of a plugin whose required config comes from the environment
must not fail.
"""

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from prodeo.errors import UnknownExtensionError
from prodeo.extensions.store import ExtensionConfigStore
from prodeo.plugins import VOICE_KINDS, ExtensionInfo, ExtensionStatus, PluginKind

#: Where a given config key's effective value came from.
ConfigSource = Literal["environment", "saved"]


class ExtensionSummary(BaseModel):
    """One installed extension as the list view shows it."""

    name: str
    #: ``None`` when the entry point failed before a manifest was resolved.
    kind: PluginKind | None = None
    version: str = ""
    status: ExtensionStatus
    #: Set only when ``status == "failed"``.
    error: str = ""
    description: str = ""
    publisher: str = ""
    homepage: str = ""
    license: str = ""
    categories: list[str] = Field(default_factory=list)
    #: Whether this extension declares a config schema worth rendering a form for.
    configurable: bool = False
    #: Voice engines run in the mjolnir client process, so the server holds no
    #: config for them; the manager shows them as installed but not editable here.
    hosted_by_client: bool = False


class ExtensionDetail(ExtensionSummary):
    """A single extension, with the schema its settings form is built from."""

    config_schema: dict[str, Any] | None = None


class ExtensionConfig(BaseModel):
    """Effective config for one extension, and where each key came from."""

    name: str
    values: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, ConfigSource] = Field(default_factory=dict)
    #: The Plugin Host loads once at boot; edits apply on the next restart.
    restart_required: bool = False


class ExtensionService:
    """Query and update installed-extension config."""

    def __init__(
        self,
        *,
        inventory_fn: Callable[[], list[ExtensionInfo]],
        env_config: dict[str, dict[str, dict[str, Any]]],
        store: ExtensionConfigStore,
    ) -> None:
        # A callable, not a snapshot: the composition root builds this while
        # wiring the API, which happens before the Plugin Host has loaded.
        self._inventory_fn = inventory_fn
        self._env_config = env_config
        self._store = store

    def list(self) -> list[ExtensionSummary]:
        return [self._summary(info) for info in self._inventory_fn()]

    def get(self, name: str) -> ExtensionDetail:
        info = self._find(name)
        return ExtensionDetail(
            **self._summary(info).model_dump(),
            config_schema=info.config_schema(),
        )

    async def config(self, name: str) -> ExtensionConfig:
        info = self._find(name)
        env = self._env_for(info)
        saved = await self._store.get(name) or {}
        merged = {**env, **saved}
        sources: dict[str, ConfigSource] = {
            key: ("saved" if key in saved else "environment") for key in merged
        }
        return ExtensionConfig(name=name, values=merged, sources=sources)

    async def set_config(self, name: str, values: dict[str, Any]) -> ExtensionConfig:
        """Validate against the declared schema, then persist the overlay.

        Raises ``pydantic.ValidationError`` when the merged config would not
        satisfy the plugin's ``config_model`` - the same check the Plugin Host
        makes at boot, so a saved config can never be one the host will reject.
        """
        info = self._find(name)
        merged = {**self._env_for(info), **values}
        if info.manifest is not None and info.manifest.config_model is not None:
            info.manifest.config_model.model_validate(merged)
        await self._store.put(name, values)
        current = await self.config(name)
        return current.model_copy(update={"restart_required": True})

    def _find(self, name: str) -> ExtensionInfo:
        for info in self._inventory_fn():
            if info.name == name:
                return info
        raise UnknownExtensionError(f"extension {name!r} is not installed")

    def _env_for(self, info: ExtensionInfo) -> dict[str, Any]:
        """The environment layer for this extension, by kind namespace."""
        if info.manifest is None:
            return {}
        return self._env_config.get(info.manifest.kind, {}).get(info.manifest.name, {})

    def _summary(self, info: ExtensionInfo) -> ExtensionSummary:
        manifest = info.manifest
        if manifest is None:
            return ExtensionSummary(name=info.name, status=info.status, error=info.error)
        return ExtensionSummary(
            name=manifest.name,
            kind=manifest.kind,
            version=manifest.version,
            status=info.status,
            error=info.error,
            description=manifest.description,
            publisher=manifest.publisher,
            homepage=manifest.homepage,
            license=manifest.license,
            categories=list(manifest.categories),
            configurable=manifest.config_model is not None,
            hosted_by_client=manifest.kind in VOICE_KINDS,
        )
