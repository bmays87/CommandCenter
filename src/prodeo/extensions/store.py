"""Persisted extension state: config overlays, disabled set, and settings.

Config reaches a plugin from two places (ADR-0014). Environment variables
(``PRODEO_PLUGINS`` / ``PRODEO_ADAPTERS`` / ``PRODEO_NOTIFY_CHANNELS``) are the
base layer: they keep CI, containers, and headless deploys reproducible. What
the extensions manager writes is the overlay, stored here, so a value edited in
the dashboard survives a restart without anyone editing a JSON blob in a shell
profile.

The seam is a Protocol so a database-backed store can replace the file without
touching the API layer; ``JsonFileConfigStore`` is the local-first default and
matches the ``prodeo.toml`` intent noted in :mod:`prodeo.config`.
"""

import json
from pathlib import Path
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field

_log = structlog.get_logger(__name__)

#: One extension's saved config, by plugin name.
ConfigMap = dict[str, dict[str, Any]]

#: Bumped when the document shape changes incompatibly. Version 1 replaced an
#: unversioned flat ``{name: config}`` map; :func:`_parse` still reads that.
STORE_VERSION = 1


class ExtensionSettings(BaseModel):
    """Manager-wide settings, as opposed to one extension's config."""

    #: Where bulk downloads (models, voices, weights) are written. Empty = the
    #: caller's default. Prompted for on first install rather than assumed,
    #: because assuming means quietly filling the system drive.
    models_dir: str = ""
    #: App extensions to launch when the server starts. Opt-in: a process that
    #: listens to a microphone should not start itself uninvited.
    autostart: list[str] = Field(default_factory=list)
    #: Entitlement for ``paid``-tier extensions. Presence is all that is
    #: checked today - this is the shape of the gate, not a verified licence,
    #: and it is deliberately not treated as a security control (ADR-0015).
    license_key: str = ""


class ExtensionState(BaseModel):
    """The whole persisted document."""

    version: int = STORE_VERSION
    config: ConfigMap = Field(default_factory=dict)
    #: Installed but deliberately not loaded. Absent = enabled.
    disabled: list[str] = Field(default_factory=list)
    settings: ExtensionSettings = Field(default_factory=ExtensionSettings)


class ExtensionConfigStore(Protocol):
    """Durable extension state the manager owns."""

    async def state(self) -> ExtensionState: ...

    async def load(self) -> ConfigMap:
        """Every saved config override, keyed by plugin name."""
        ...

    async def get(self, name: str) -> dict[str, Any] | None:
        """One plugin's saved override, or ``None`` when never written."""
        ...

    async def put(self, name: str, config: dict[str, Any]) -> None:
        """Replace one plugin's override. Callers validate before writing."""
        ...

    async def delete(self, name: str) -> None:
        """Drop an override so the environment layer applies again."""
        ...

    async def set_enabled(self, name: str, enabled: bool) -> None: ...

    async def disabled(self) -> set[str]: ...

    async def settings(self) -> ExtensionSettings: ...

    async def put_settings(self, settings: ExtensionSettings) -> None: ...


class JsonFileConfigStore:
    """``ExtensionConfigStore`` backed by a single JSON file.

    Writes are whole-file and atomic (temp file + replace), which is right for
    the single-user, single-process deployment v1 targets and keeps the file
    hand-editable. Lives under ``PRODEO_DATA_DIR``, deliberately outside the
    virtualenv so ``uv sync`` cannot delete it.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: ExtensionState | None = None

    async def state(self) -> ExtensionState:
        if self._cache is None:
            self._cache = self._read()
        return self._cache.model_copy(deep=True)

    async def load(self) -> ConfigMap:
        return (await self.state()).config

    async def get(self, name: str) -> dict[str, Any] | None:
        return (await self.state()).config.get(name)

    async def put(self, name: str, config: dict[str, Any]) -> None:
        state = await self.state()
        state.config[name] = config
        self._write(state)

    async def delete(self, name: str) -> None:
        state = await self.state()
        if state.config.pop(name, None) is not None:
            self._write(state)

    async def set_enabled(self, name: str, enabled: bool) -> None:
        state = await self.state()
        current = set(state.disabled)
        if enabled:
            current.discard(name)
        else:
            current.add(name)
        state.disabled = sorted(current)
        self._write(state)

    async def disabled(self) -> set[str]:
        return set((await self.state()).disabled)

    async def settings(self) -> ExtensionSettings:
        return (await self.state()).settings

    async def put_settings(self, settings: ExtensionSettings) -> None:
        state = await self.state()
        state.settings = settings
        self._write(state)

    def _read(self) -> ExtensionState:
        if not self._path.exists():
            return ExtensionState()
        try:
            parsed = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt or unreadable overlay must not stop the server booting:
            # the environment layer alone is a working configuration.
            _log.exception("extensions.state_unreadable", path=str(self._path))
            return ExtensionState()
        return _parse(parsed, path=self._path)

    def _write(self, state: ExtensionState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp.write_text(json.dumps(state.model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)
        self._cache = state.model_copy(deep=True)


def _parse(parsed: Any, *, path: Path) -> ExtensionState:
    """Read either the current document or the original unversioned one."""
    if not isinstance(parsed, dict):
        _log.warning("extensions.state_not_an_object", path=str(path))
        return ExtensionState()
    if "version" not in parsed:
        # The first shipped format was a bare {name: config} map. Read it so an
        # upgrade never silently discards settings someone already saved.
        legacy = {k: v for k, v in parsed.items() if isinstance(v, dict)}
        _log.info("extensions.state_migrated", path=str(path), extensions=len(legacy))
        return ExtensionState(config=legacy)
    try:
        return ExtensionState.model_validate(parsed)
    except ValueError:
        _log.exception("extensions.state_invalid", path=str(path))
        return ExtensionState()
