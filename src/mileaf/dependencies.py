"""Public dependency seams used by routes and HTTP contract tests."""

from datetime import UTC, datetime
from typing import Annotated

import asyncpg
from fastapi import Path, Request

from mileaf.cache import TTLCache


def get_now() -> datetime:
    return datetime.now(UTC)


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_cache(request: Request) -> TTLCache:
    return request.app.state.cache


VehicleID = Annotated[int, Path(gt=0)]
