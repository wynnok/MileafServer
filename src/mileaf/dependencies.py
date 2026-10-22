"""Public dependency seams used by routes and HTTP contract tests."""

from datetime import UTC, datetime
from typing import Annotated

import asyncpg
from fastapi import Path, Request


def get_now() -> datetime:
    return datetime.now(UTC)


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


VehicleID = Annotated[int, Path(gt=0)]
