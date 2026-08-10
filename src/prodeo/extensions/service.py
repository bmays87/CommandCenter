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

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from prodeo.environment import Environment, detect
from prodeo.errors import NotEntitledError, RequirementsNotMetError, UnknownExtensionError
from prodeo.events import Event, new_event
from prodeo.events import types as ev
from prodeo.extensions.catalog import (
    Catalog,
    CatalogEntry,
    ExtensionCatalog,
    unmet_requirements,
)
from prodeo.extensions.installer import InstallResult, PackageInstaller
from prodeo.extensions.store import ExtensionConfigStore, ExtensionSettings
from prodeo.plugins import VOICE_KINDS, ExtensionInfo, ExtensionStatus, PluginKind

_SOURCE = "extensions"

#: Where a given config key's effective value came from.
ConfigSource = Literal["environment", "saved"]

#: The bus's publish, injected rather than imported - services never import
#: each other (CLAUDE.md).
PublishFn = Callable[[Event], Awaitable[None]]


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
    """Query and update installed extensions: config, enablement, installation."""

    def __init__(
        self,
        *,
        inventory_fn: Callable[[], list[ExtensionInfo]],
        env_config: dict[str, dict[str, dict[str, Any]]],
        store: ExtensionConfigStore,
        catalog: ExtensionCatalog | None = None,
        installer: PackageInstaller | None = None,
        publish: PublishFn | None = None,
        node: str = "local",
        env: Environment | None = None,
        default_models_dir: Path | None = None,
    ) -> None:
        # Injected so tests never depend on whether the host has a GPU.
        self._env = env or detect()
        self._default_models_dir = default_models_dir
        # A callable, not a snapshot: the composition root builds this while
        # wiring the API, which happens before the Plugin Host has loaded.
        self._inventory_fn = inventory_fn
        self._env_config = env_config
        self._store = store
        self._catalog = catalog
        self._installer = installer
        self._publish = publish
        self._node = node

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

    # --- enablement -------------------------------------------------------

    async def set_enabled(self, name: str, enabled: bool) -> ExtensionSummary:
        """Turn an installed extension on or off for the next boot.

        Not live: the Plugin Host loads once at start, so a disabled plugin
        keeps running until a restart. The summary reports the *stored*
        intention, which is what the UI should show.
        """
        info = self._find(name)
        await self._store.set_enabled(name, enabled)
        summary = self._summary(info)
        if not enabled:
            summary = summary.model_copy(update={"status": "disabled"})
        return summary

    # --- installation -----------------------------------------------------

    async def install(self, name: str) -> InstallResult:
        """Install a **catalog** extension by name.

        The caller never supplies a package spec: the name is resolved against
        the sanctioned catalog and the spec comes from there, so this endpoint
        cannot be used to install arbitrary code (ADR-0015).
        """
        entry = await self._catalog_entry(name)
        # Both gates run before the installer is touched: Parakeet downloads
        # ~250MB before it would otherwise notice there is no GPU.
        self._check_requirements(entry)
        await self._check_entitlement(entry)
        installer = self._require_installer()
        result = await installer.install(entry.package)
        await self._emit(
            ev.SYSTEM_EXTENSION_INSTALLED if result.ok else ev.SYSTEM_EXTENSION_INSTALL_FAILED,
            {
                "extension": name,
                "package": entry.package,
                "version": entry.version,
                **({} if result.ok else {"error": result.error}),
            },
        )
        return result

    async def uninstall(self, name: str) -> InstallResult:
        """Remove a catalog extension's distribution from the extension dir."""
        entry = await self._catalog_entry(name)
        installer = self._require_installer()
        result = await installer.uninstall(entry.package)
        if result.ok:
            # Drop its saved state too, so a reinstall starts clean rather than
            # inheriting settings for a version that may no longer have them.
            await self._store.delete(name)
            await self._store.set_enabled(name, True)
            await self._emit(
                ev.SYSTEM_EXTENSION_UNINSTALLED,
                {"extension": name, "package": entry.package},
            )
        return result

    # --- manager settings -------------------------------------------------

    async def settings(self) -> ExtensionSettings:
        settings = await self._store.settings()
        if not settings.models_dir and self._default_models_dir:
            settings.models_dir = str(self._default_models_dir)
        return settings

    async def set_settings(self, settings: ExtensionSettings) -> ExtensionSettings:
        await self._store.put_settings(settings)
        await self._apply_config_defaults(settings.models_dir)
        return await self.settings()

    async def _apply_config_defaults(self, models_dir: str) -> None:
        """Push the chosen models root into every engine that has a cache knob.

        One decision, honoured everywhere - the roadmap's rule that a bulk
        write never silently assumes the system drive. ``setdefault``
        semantics: a value the user set explicitly is never overwritten.
        """
        if not models_dir or self._catalog is None:
            return
        catalog = await self._catalog.fetch()
        for entry in catalog.entries:
            if not entry.config_defaults:
                continue
            saved = dict(await self._store.get(entry.name) or {})
            changed = False
            for key, template in entry.config_defaults.items():
                if not saved.get(key):
                    saved[key] = template.replace("{models_dir}", models_dir)
                    changed = True
            if changed:
                await self._store.put(entry.name, saved)

    async def catalog(self) -> Catalog:
        """The sanctioned index, each entry annotated for *this* machine."""
        if self._catalog is None:
            raise UnknownExtensionError("no extension catalog is configured")
        catalog = await self._catalog.fetch()
        return catalog.model_copy(
            update={
                "entries": [
                    entry.model_copy(update={"unmet": unmet_requirements(entry, self._env)})
                    for entry in catalog.entries
                ]
            }
        )

    def _check_requirements(self, entry: CatalogEntry) -> None:
        """Refuse an extension this machine cannot run."""
        unmet = unmet_requirements(entry, self._env)
        if unmet:
            raise RequirementsNotMetError(f"{entry.name!r} cannot run here: {'; '.join(unmet)}")

    async def _check_entitlement(self, entry: CatalogEntry) -> None:
        """Refuse a paid extension without an entitlement.

        The check is presence of a licence key and nothing more. That is
        deliberate and documented (ADR-0015): it gives the tier a real,
        testable behaviour and puts the decision in one place, so replacing it
        with a verified entitlement later touches this method only. It is not
        a security control and must not be described as one.
        """
        if entry.tier != "paid":
            return
        settings = await self._store.settings()
        if not settings.license_key.strip():
            raise NotEntitledError(
                entry.tier_note or f"{entry.name!r} is a paid extension and needs a licence key"
            )

    async def _catalog_entry(self, name: str) -> CatalogEntry:
        if self._catalog is None:
            raise UnknownExtensionError("no extension catalog is configured")
        catalog = await self._catalog.fetch()
        for entry in catalog.entries:
            if entry.name == name and entry.package:
                return entry
        raise UnknownExtensionError(f"{name!r} is not in the extension catalog")

    def _require_installer(self) -> PackageInstaller:
        if self._installer is None:
            raise UnknownExtensionError("installation is not available on this server")
        return self._installer

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._publish is not None:
            await self._publish(
                new_event(event_type, node=self._node, source=_SOURCE, payload=payload)
            )

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
