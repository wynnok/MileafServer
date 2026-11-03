"""Environment-only deployment settings."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MILEAF_", env_file=None)

    database_dsn: SecretStr = Field(min_length=1)
    state_file: Path = Path("/data/state.json")
    listen_address: str = "0.0.0.0:8080"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    max_concurrency: int = Field(default=4, ge=1)
    cache_ttl_seconds: int = Field(default=30, ge=0)
    charging_cost_writes_enabled: bool = False
    cache_capacity: int = Field(default=256, ge=1)

    @field_validator("database_dsn")
    @classmethod
    def validate_database_dsn(cls, value: SecretStr) -> SecretStr:
        try:
            parsed = urlsplit(value.get_secret_value())
            valid = parsed.scheme in {"postgres", "postgresql"} and bool(parsed.netloc)
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Expected a PostgreSQL connection URL")
        return value

    @field_validator("listen_address")
    @classmethod
    def validate_listen_address(cls, value: str) -> str:
        host, separator, port = value.rpartition(":")
        if not separator or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Expected host:port with port between 1 and 65535")
        return value
