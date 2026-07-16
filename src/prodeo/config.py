"""Runtime configuration via Pydantic Settings.

Phase 1 reads environment variables (prefix ``PRODEO_``) and defaults;
``prodeo.toml`` support arrives alongside the plugin host.
"""

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRODEO_")

    node_name: str = "local"
    data_dir: Path = Path.home() / ".local" / "share" / "prodeo"
    log_level: str = "INFO"

    # API
    api_host: str = "127.0.0.1"
    api_port: int = 8600
    #: v1 single-token auth. Unset = open API (localhost development only).
    api_token: str | None = None
    #: Where the built dashboard lives; served at ``/`` when present.
    dashboard_dir: Path = Path("dashboard") / "dist"

    # Mediation
    #: Seconds before an unanswered interaction auto-resolves (permissions
    #: auto-deny; questions expire). Unset = interactions wait forever.
    mediation_default_timeout_s: float | None = None

    # Notifications
    #: Event type pattern -> channel names. From the environment this is JSON,
    #: e.g. ``PRODEO_NOTIFY_RULES='{"interaction.requested": ["ntfy"]}'``.
    notify_rules: dict[str, list[str]] = {
        "interaction.requested": ["log"],
        "session.completed": ["log"],
        "session.failed": ["log"],
    }
    #: Channel name -> channel config, e.g.
    #: ``PRODEO_NOTIFY_CHANNELS='{"ntfy": {"topic": "my-agents"}}'``.
    notify_channels: dict[str, dict[str, Any]] = {}
    #: Public base URL of this server's dashboard, used for notification
    #: click-throughs (e.g. ``https://prodeo.example.com``).
    public_url: str = ""

    # Adapters
    #: Per-adapter config, keyed by adapter name. From the environment this is
    #: JSON, e.g. ``PRODEO_ADAPTERS='{"claude-code": {"idle_timeout_s": 600}}'``.
    adapters: dict[str, dict[str, Any]] = {}
    #: How often adapters re-scan for new sessions (0 disables the loop).
    discovery_interval_s: float = 10.0

    @property
    def event_db_path(self) -> Path:
        return self.data_dir / "events.db"
