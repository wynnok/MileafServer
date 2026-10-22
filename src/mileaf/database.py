"""Bounded, read-only PostgreSQL connections."""

import asyncpg

from mileaf.config import Settings


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
