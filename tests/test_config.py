from __future__ import annotations

import pytest
from pydantic import ValidationError

from mileaf.config import Settings


def test_production_rejects_weak_secrets() -> None:
    with pytest.raises(ValidationError):
        Settings(
            env="prod",
            jwt_secret="short",
            agent_token_pepper="x" * 32,
        )

    with pytest.raises(ValidationError):
        Settings(
            env="prod",
            jwt_secret="x" * 32,
            agent_token_pepper="change-me",
        )


def test_cors_origins_are_split() -> None:
    settings = Settings(cors_origins="https://app.example.com, https://admin.example.com")
    assert settings.cors_origin_list == [
        "https://app.example.com",
        "https://admin.example.com",
    ]
