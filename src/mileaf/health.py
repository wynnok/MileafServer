from importlib.metadata import version
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends

from mileaf.dependencies import get_pool
from mileaf.errors import APIError, ErrorCode

router = APIRouter(prefix="/v1")


@router.get("/health")
async def health(pool: Annotated[asyncpg.Pool, Depends(get_pool)]) -> dict[str, str]:
    try:
        connection = await pool.acquire(timeout=5)
    except TimeoutError:
        raise APIError(503, ErrorCode.DB_UNAVAILABLE, "Database is unavailable") from None
    try:
        await connection.fetchval("SELECT 1")
    finally:
        await pool.release(connection)
    return {"status": "ok", "version": version("mileaf-server")}
