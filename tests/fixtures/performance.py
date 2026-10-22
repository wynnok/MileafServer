"""Synthetic E1 data, imported only by opt-in slow fixtures. Never use a real DSN."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from random import Random

import asyncpg

RANDOM_SEED = 27
VEHICLE_ID = 3
FIRST_DRIVE_ID = 100_000
FIRST_POSITION_ID = 1_000_000
FIRST_SESSION_ID = 100_000
START = datetime(2025, 4, 1)
STEP = (datetime(2026, 3, 15, 10) - START) / 20_000


async def load_performance_seed(db: asyncpg.Connection) -> int:
    """Add 20k drives, 200k positions and 5k sessions with 20 samples each.

    A fresh fixture database (or rolled-back transaction) reproduces identical rows.
    IDs occupy a range separate from the base seeds. No production schema changes,
    indexes or dependencies are needed. COPY keeps generation outside request timing.
    """
    random = Random(RANDOM_SEED)
    # Explicit IDs also reproduce after rollback: PostgreSQL sequences do not roll back.
    vehicle_id = VEHICLE_ID
    await db.execute("INSERT INTO car_settings(id) VALUES ($1)", vehicle_id)
    await db.execute(
        "INSERT INTO cars(id,eid,vid,vin,name,model,efficiency,settings_id,inserted_at,updated_at) "
        "VALUES ($1::smallint,1003,2003,'FIXTURE00000000003','性能测试车','3',0.15,"
        "$1::smallint,'2025-04-01','2025-04-01')",
        vehicle_id,
    )
    await db.copy_records_to_table(
        "drives",
        columns=(
            "id",
            "car_id",
            "start_date",
            "end_date",
            "distance",
            "duration_min",
            "start_rated_range_km",
            "end_rated_range_km",
            "speed_max",
        ),
        records=(
            (
                FIRST_DRIVE_ID + index,
                vehicle_id,
                START + STEP * index,
                START + STEP * index + timedelta(minutes=30),
                random.uniform(5, 60),
                30,
                Decimal(350),
                Decimal(325),
                110,
            )
            for index in range(20_000)
        ),
    )

    def positions() -> Iterator[tuple[object, ...]]:
        position_id = FIRST_POSITION_ID
        for index in range(20_000):
            # One dense drive exercises SQL + LTTB; the others share the remaining 180k.
            count = 20_000 if index == 0 else 10 if index <= 9 else 9
            for point in range(count):
                progress = point / (count - 1)
                yield (
                    position_id,
                    vehicle_id,
                    FIRST_DRIVE_ID + index,
                    START + STEP * index + timedelta(minutes=30 * progress),
                    Decimal("31.23"),
                    Decimal("121.47"),
                    random.randrange(0, 111),
                    80 - int(10 * progress),
                    random.randrange(0, 100),
                    Decimal(350) - Decimal(25) * Decimal(str(progress)),
                    10_000 + index * 20 + progress * 20,
                )
                position_id += 1

    await db.copy_records_to_table(
        "positions",
        columns=(
            "id",
            "car_id",
            "drive_id",
            "date",
            "latitude",
            "longitude",
            "speed",
            "battery_level",
            "elevation",
            "rated_battery_range_km",
            "odometer",
        ),
        records=positions(),
    )
    await db.execute(
        "UPDATE drives d SET start_position_id=p.first_id, end_position_id=p.last_id "
        "FROM (SELECT drive_id, min(id) first_id, max(id) last_id FROM positions "
        "WHERE car_id=$1 GROUP BY drive_id) p WHERE d.id=p.drive_id",
        vehicle_id,
    )
    await db.copy_records_to_table(
        "charging_processes",
        columns=(
            "id",
            "car_id",
            "position_id",
            "start_date",
            "end_date",
            "charge_energy_added",
            "charge_energy_used",
            "cost",
            "duration_min",
            "start_battery_level",
            "end_battery_level",
        ),
        records=(
            (
                FIRST_SESSION_ID + index,
                vehicle_id,
                FIRST_POSITION_ID,
                START + STEP * (index * 4) + timedelta(minutes=30),
                START + STEP * (index * 4) + timedelta(minutes=90),
                Decimal(20),
                Decimal(22),
                Decimal(12),
                60,
                40,
                70,
            )
            for index in range(5_000)
        ),
    )
    await db.copy_records_to_table(
        "charges",
        columns=(
            "id",
            "charging_process_id",
            "date",
            "charge_energy_added",
            "charger_power",
            "charger_phases",
            "battery_level",
            "ideal_battery_range_km",
        ),
        records=(
            (
                FIRST_POSITION_ID + session * 20 + point,
                FIRST_SESSION_ID + session,
                START + STEP * (session * 4) + timedelta(minutes=30 + 60 * point / 19),
                Decimal(20) * point / 19,
                120 if session % 2 else 11,
                None if session % 2 else 3,
                40 + int(30 * point / 19),
                Decimal(300),
            )
            for session in range(5_000)
            for point in range(20)
        ),
    )
    # Match a settled database rather than an empty planner statistics snapshot.
    for table in ("cars", "drives", "positions", "charging_processes", "charges"):
        await db.execute(f"ANALYZE {table}")
    return vehicle_id
