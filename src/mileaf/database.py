"""Bounded, read-only PostgreSQL connections."""

import asyncpg

from mileaf.config import Settings
from mileaf.errors import APIError, ErrorCode


async def create_pool(settings: Settings) -> asyncpg.Pool:
    # Startup parameters remain defaults after asyncpg resets pooled connections.
    return await asyncpg.create_pool(
        settings.database_dsn.get_secret_value(),
        min_size=0,
        max_size=settings.max_concurrency,
        timeout=5,
        command_timeout=5,
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": "5s",
            "idle_in_transaction_session_timeout": "5s",
        },
    )


async def fetch_rows(db: asyncpg.Connection, query: str, *args: object) -> list[asyncpg.Record]:
    """Bound each primitive query, including connections supplied outside the runtime pool."""
    try:
        async with db.transaction(readonly=True):
            await db.execute("SET LOCAL statement_timeout = '5s'")
            return await db.fetch(query, *args, timeout=5)
    except (TimeoutError, asyncpg.QueryCanceledError):
        raise APIError(503, ErrorCode.UPSTREAM_TIMEOUT, "Database query timed out") from None
