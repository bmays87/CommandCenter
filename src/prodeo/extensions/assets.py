"""Downloading the model files an extension needs before it can run.

Some engines fetch their own weights on first use; others do not, and a missing
file is the difference between a working voice client and one that crash-loops.
Piper is the sharp case: ``voice_path`` is required with no default, and a
*present but wrong* path is worse still - Mjölnir starts cleanly and is
silently mute, because synthesis failures are swallowed in its pipeline.

So an asset declares what it **produces**, and this module treats an asset as
present only when every produced file exists. That is what catches the missing
``.onnx.json`` sibling, which is the actual shape of the silent-mute bug.

Assets live in the catalog, which is data. Core runs a declared command with a
closed set of substitutions and writes the resulting path into the app's config
at a declared pointer - so nothing here knows what Piper is.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from prodeo.errors import UnknownExtensionError
from prodeo.extensions.catalog import ExtensionAsset, ExtensionCatalog
from prodeo.extensions.store import ExtensionConfigStore

_log = structlog.get_logger(__name__)

#: Bound on one download. Voices are tens of MB, speech models a few hundred.
DEFAULT_TIMEOUT_S = 1800.0


class AssetStatus(BaseModel):
    """One downloadable asset and whether this machine already has it."""

    id: str
    label: str = ""
    approx_mb: int = 0
    #: Every file the asset must produce, resolved for this machine.
    produces: list[str] = Field(default_factory=list)
    #: Which produced files are missing. Empty = ready to use.
    missing: list[str] = Field(default_factory=list)
    present: bool = False


class AssetResult(BaseModel):
    """The outcome of a download attempt."""

    ok: bool
    asset: str
    output: str = ""
    error: str = ""
    #: Where the asset landed, when the download succeeded.
    path: str = ""
    #: The app config key this was written into, if the asset declares one.
    configured: str = ""


class AssetProvisioner:
    """Resolves, checks, and downloads the assets declared by the catalog."""

    def __init__(
        self,
        *,
        catalog: ExtensionCatalog,
        store: ExtensionConfigStore,
        models_dir_fn: Any,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        python: str | None = None,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._models_dir_fn = models_dir_fn
        self._timeout_s = timeout_s
        self._python = python or sys.executable

    async def list_assets(self, name: str) -> list[AssetStatus]:
        """Every asset for one extension, with what is already on disk."""
        entry = await self._entry(name)
        return [self._status(asset, await self._substitutions()) for asset in entry.assets]

    async def download(self, name: str, asset_id: str) -> AssetResult:
        """Fetch one asset, then wire its path into the owning app's config."""
        entry = await self._entry(name)
        asset = next((a for a in entry.assets if a.id == asset_id), None)
        if asset is None:
            raise UnknownExtensionError(f"{name!r} declares no asset {asset_id!r}")

        subs = await self._substitutions()
        status = self._status(asset, subs)
        if status.present:
            # Already here. Still wire the config, so a re-run of the wizard
            # after a manual download does the right thing.
            return await self._apply_config(asset, status, output="already present")

        argv = [_substitute(part, subs) for part in asset.command]
        if not argv:
            return AssetResult(ok=False, asset=asset_id, error="asset declares no command")
        Path(_substitute(asset.into or "{models_dir}", subs)).mkdir(parents=True, exist_ok=True)

        result = await self._run(argv, asset_id)
        if not result.ok:
            return result

        # Trust the declared outputs, not the exit code: a download that
        # "succeeded" without producing its files is the silent-mute failure.
        after = self._status(asset, subs)
        if not after.present:
            return AssetResult(
                ok=False,
                asset=asset_id,
                output=result.output,
                error="download finished but these files are missing: " + ", ".join(after.missing),
            )
        return await self._apply_config(asset, after, output=result.output)

    async def _entry(self, name: str) -> Any:
        catalog = await self._catalog.fetch()
        for entry in catalog.entries:
            if entry.name == name:
                return entry
        raise UnknownExtensionError(f"{name!r} is not in the extension catalog")

    async def _substitutions(self) -> dict[str, str]:
        models_dir = self._models_dir_fn()
        if asyncio.iscoroutine(models_dir):
            models_dir = await models_dir
        return {"python": self._python, "models_dir": str(models_dir)}

    def _status(self, asset: ExtensionAsset, subs: dict[str, str]) -> AssetStatus:
        produces = [_substitute(p, subs) for p in asset.produces]
        missing = [p for p in produces if not Path(p).exists()]
        return AssetStatus(
            id=asset.id,
            label=asset.label,
            approx_mb=asset.approx_mb,
            produces=produces,
            missing=missing,
            present=bool(produces) and not missing,
        )

    async def _apply_config(
        self, asset: ExtensionAsset, status: AssetStatus, *, output: str
    ) -> AssetResult:
        path = status.produces[0] if status.produces else ""
        configured = ""
        if asset.config_app and asset.config_pointer:
            config = dict(await self._store.get(asset.config_app) or {})
            _set_pointer(config, asset.config_pointer, path)
            await self._store.put(asset.config_app, config)
            configured = f"{asset.config_app}:{asset.config_pointer}"
            _log.info("assets.configured", app=asset.config_app, pointer=asset.config_pointer)
        return AssetResult(ok=True, asset=asset.id, output=output, path=path, configured=configured)

    async def _run(self, argv: list[str], asset_id: str) -> AssetResult:
        _log.info("assets.download_started", asset=asset_id, command=argv)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            _log.exception("assets.download_failed", asset=asset_id)
            return AssetResult(ok=False, asset=asset_id, error=str(exc))

        try:
            async with asyncio.timeout(self._timeout_s):
                stdout, _ = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            return AssetResult(
                ok=False, asset=asset_id, error=f"timed out after {self._timeout_s:.0f}s"
            )

        output = stdout.decode(errors="replace").strip()[-2000:]
        if process.returncode != 0:
            return AssetResult(
                ok=False,
                asset=asset_id,
                output=output,
                error=f"download exited with {process.returncode}",
            )
        return AssetResult(ok=True, asset=asset_id, output=output)


def _substitute(text: str, subs: dict[str, str]) -> str:
    """Fill the closed set of placeholders. Unknown braces are left alone."""
    for key, value in subs.items():
        text = text.replace("{" + key + "}", value)
    return text


def _set_pointer(config: dict[str, Any], pointer: str, value: Any) -> None:
    """Set a dotted path, creating intermediate objects.

    ``engines.piper.voice_path`` reaches into the nested ``engines`` map an
    app's settings use, without core knowing what any of those names mean.
    """
    parts = pointer.split(".")
    node = config
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
