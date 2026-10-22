"""Deterministic, independently replayable domain seed modules."""

from collections.abc import Callable, Coroutine
from typing import Any

import asyncpg

from . import charging, drives, firmware, locations, vehicles

SEEDERS: tuple[Callable[[asyncpg.Connection], Coroutine[Any, Any, None]], ...] = (
    vehicles.seed,
    locations.seed,
    drives.seed,
    charging.seed,
    firmware.seed,
)


async def load_seeds(db: asyncpg.Connection) -> None:
    for seed in SEEDERS:
        await seed(db)
    # Explicit scenario IDs must not collide with helpers using sequence defaults.
    for table in (
        "cars",
        "car_settings",
        "settings",
        "addresses",
        "geofences",
        "drives",
        "positions",
        "states",
        "charging_processes",
        "charges",
        "updates",
    ):
        await db.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
            f"coalesce((SELECT max(id) FROM {table}), 1))"
        )
