"""Persisted per-extension config.

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

_log = structlog.get_logger(__name__)

#: One extension's saved config, by plugin name.
ConfigMap = dict[str, dict[str, Any]]


class ExtensionConfigStore(Protocol):
    """Durable per-extension config the extensions manager owns."""

    async def load(self) -> ConfigMap:
        """Every saved override, keyed by plugin name."""
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


class JsonFileConfigStore:
    """``ExtensionConfigStore`` backed by a single JSON file.

    Writes are whole-file and atomic (temp file + replace), which is right for
    the single-user, single-process deployment v1 targets and keeps the file
    hand-editable. Lives under ``PRODEO_DATA_DIR``, deliberately outside the
    virtualenv so ``uv sync`` cannot delete it.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: ConfigMap | None = None

    async def load(self) -> ConfigMap:
        if self._cache is None:
            self._cache = self._read()
        return dict(self._cache)

    async def get(self, name: str) -> dict[str, Any] | None:
        return (await self.load()).get(name)

    async def put(self, name: str, config: dict[str, Any]) -> None:
        data = await self.load()
        data[name] = config
        self._write(data)

    async def delete(self, name: str) -> None:
        data = await self.load()
        if data.pop(name, None) is not None:
            self._write(data)

    def _read(self) -> ConfigMap:
        if not self._path.exists():
            return {}
        try:
            parsed = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt or unreadable overlay must not stop the server booting:
            # the environment layer alone is a working configuration.
            _log.exception("extensions.config_unreadable", path=str(self._path))
            return {}
        if not isinstance(parsed, dict):
            _log.warning("extensions.config_not_an_object", path=str(self._path))
            return {}
        return {k: v for k, v in parsed.items() if isinstance(v, dict)}

    def _write(self, data: ConfigMap) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)
        self._cache = dict(data)
