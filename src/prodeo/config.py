"""Runtime configuration via Pydantic Settings.

Phase 0 reads environment variables (prefix ``PRODEO_``) and defaults;
``prodeo.toml`` support arrives alongside the plugin host.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRODEO_")

    node_name: str = "local"
    data_dir: Path = Path.home() / ".local" / "share" / "prodeo"
    log_level: str = "INFO"

    @property
    def event_db_path(self) -> Path:
        return self.data_dir / "events.db"
