"""App extensions: separate-process clients the server can supervise (ADR-0014)."""

from prodeo.apps.manifest import (
    APP_API_VERSION,
    APP_ENTRY_POINT_GROUP,
    AppManifest,
    config_to_env,
    installed_apps,
    resolve_app_manifest,
)
from prodeo.apps.supervisor import AppState, AppStatus, AppSupervisor

__all__ = [
    "APP_API_VERSION",
    "APP_ENTRY_POINT_GROUP",
    "AppManifest",
    "AppState",
    "AppStatus",
    "AppSupervisor",
    "config_to_env",
    "installed_apps",
    "resolve_app_manifest",
]
