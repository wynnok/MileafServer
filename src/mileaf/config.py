from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from MILEAF_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="MILEAF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://mileaf:mileaf@localhost:5432/mileaf"
    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    refresh_token_days: int = 30
    agent_token_pepper: str = "dev-only-change-me"
    pairing_code_minutes: int = 15
    auto_create_tables: bool = False
    cors_origins: str = "*"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> Settings:
        if self.env != "prod":
            return self
        if len(self.jwt_secret) < 32 or "change-me" in self.jwt_secret:
            raise ValueError("MILEAF_JWT_SECRET must be a strong random value in production")
        if len(self.agent_token_pepper) < 32 or "change-me" in self.agent_token_pepper:
            raise ValueError(
                "MILEAF_AGENT_TOKEN_PEPPER must be a strong random value in production"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [item.strip() for item in self.cors_origins.split(",") if item.strip()]
        return origins or ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
