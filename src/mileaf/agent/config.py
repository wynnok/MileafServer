from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Configuration for the on-premises TeslaMate connector."""

    model_config = SettingsConfigDict(
        env_prefix="MILEAF_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    teslamate_database_url: str = "postgresql://teslamate:secret@localhost:5432/teslamate"
    mileaf_url: str = "http://localhost:8000"
    pairing_code: str | None = None
    agent_token: str | None = None
    state_file: str = "/data/agent-state.json"
    interval_seconds: int = Field(default=300, ge=30)
    batch_size: int = Field(default=200, ge=1, le=1000)
    full_sync_interval_hours: int = Field(default=24, ge=1)
    source_name: str = "Mileaf Agent"
    teslamate_version: str | None = None
    request_timeout_seconds: float = 30.0
    log_level: str = "INFO"

    @property
    def asyncpg_dsn(self) -> str:
        return self.teslamate_database_url.replace("postgresql+asyncpg://", "postgresql://")
