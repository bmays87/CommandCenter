"""App extensions: what a separate-process client declares about itself.

The second extension class (ADR-0014). A ``plugin`` is loaded in-process by a
host; an ``app`` is its own process that talks to the server over the same HTTP
and WebSocket API the dashboard uses, so it has no host to be loaded *by*.
Mjölnir is the first one.

Discovery mirrors :mod:`prodeo.plugins` exactly - an entry point in the
``prodeo.apps`` group resolving to an :class:`AppManifest` - so the manifest,
config-schema, and version-handshake machinery is the one that already exists.

**Core stays generic.** Nothing here knows a Mjölnir field name. An app that
reads its settings through pydantic-settings declares ``env_prefix``, and the
supervisor derives the whole child environment from that plus the saved config.
"""

import json
from collections.abc import Callable, Iterable
from importlib.metadata import entry_points
from typing import Any, Final, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

_log = structlog.get_logger(__name__)

APP_ENTRY_POINT_GROUP: Final = "prodeo.apps"

#: Bumped when the app manifest/supervisor contract changes incompatibly.
APP_API_VERSION: Final = 1


class AppManifest(BaseModel):
    """What an app-class extension declares about itself."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    version: str
    app_api_version: int = APP_API_VERSION
    #: Pydantic settings model. Its JSON Schema drives the setup form, exactly
    #: as ``config_model`` does for plugins.
    config_model: type[BaseModel] | None = None
    #: argv to launch. ``command[0]`` is resolved against PATH and the server's
    #: own scripts directory.
    command: list[str] = Field(default_factory=list)
    #: pydantic-settings prefix. A config key ``api_token`` with prefix
    #: ``MJOLNIR_`` becomes ``MJOLNIR_API_TOKEN`` in the child's environment.
    #: This one field is what keeps the supervisor free of app-specific code.
    env_prefix: str = ""
    #: Config fields the setup wizard shows first; the rest go under
    #: "Advanced". Mjölnir has 33 settings and must not become a 33-field form.
    setup_fields: list[str] = Field(default_factory=list)
    #: ``client_id`` this app heartbeats with, so the supervisor can report
    #: liveness from presence rather than guessing from the process alone.
    presence_client_id: str = ""
    #: Config field that should default to this server's own URL, so a locally
    #: supervised client is not asked where its server is. Named by the app;
    #: core never assumes a field name.
    server_url_field: str = ""
    #: Config field that should default to this server's API token.
    api_token_field: str = ""

    # Presentation, same shape as PluginManifest.
    description: str = ""
    publisher: str = ""
    homepage: str = ""
    license: str = ""
    categories: list[str] = Field(default_factory=list)


class _EntryPointLike(Protocol):
    """The slice of ``importlib.metadata.EntryPoint`` discovery uses."""

    @property
    def name(self) -> str: ...

    def load(self) -> Any: ...


def _installed_entry_points() -> Iterable[_EntryPointLike]:
    return entry_points(group=APP_ENTRY_POINT_GROUP)


def resolve_app_manifest(ep: _EntryPointLike) -> AppManifest:
    """Load an entry point and normalize it to an :class:`AppManifest`."""
    obj = ep.load()
    manifest = obj if isinstance(obj, AppManifest) else obj()
    if not isinstance(manifest, AppManifest):
        raise TypeError(
            f"entry point {ep.name!r} must resolve to an AppManifest, got {type(manifest).__name__}"
        )
    if manifest.app_api_version != APP_API_VERSION:
        raise RuntimeError(
            f"app API version mismatch: {manifest.name!r} declares "
            f"{manifest.app_api_version}, core provides {APP_API_VERSION}"
        )
    return manifest


def installed_apps(
    entry_points_fn: Callable[[], Iterable[_EntryPointLike]] = _installed_entry_points,
) -> list[AppManifest]:
    """Every installed app extension. A bad entry point is skipped, not fatal.

    An app that cannot be described is one the user cannot start, which is a
    degraded dashboard - never a reason to fail the server's boot.
    """
    found: list[AppManifest] = []
    for ep in entry_points_fn():
        try:
            found.append(resolve_app_manifest(ep))
        except Exception:
            _log.exception("apps.manifest_unresolvable", entry_point=ep.name)
    return found


def config_to_env(manifest: AppManifest, config: dict[str, Any]) -> dict[str, str]:
    """Render saved config as environment variables for the child process.

    pydantic-settings reads ``<PREFIX><FIELD>`` in upper case and parses
    non-string values as JSON, so strings pass through untouched and everything
    else is JSON-encoded. This is the whole of the app-config contract, and why
    the supervisor needs no knowledge of any particular app's fields.
    """
    if not manifest.env_prefix:
        return {}
    env: dict[str, str] = {}
    for key, value in config.items():
        name = f"{manifest.env_prefix}{key}".upper()
        env[name] = value if isinstance(value, str) else json.dumps(value)
    return env
