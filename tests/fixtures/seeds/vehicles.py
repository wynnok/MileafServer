"""Vehicles, global settings and state segments. All source distances remain km."""

from datetime import datetime

import asyncpg


async def seed_car(
    db: asyncpg.Connection,
    *,
    efficiency: float | None = None,
    unit_of_length: str = "km",
    name: str | None = None,
) -> int:
    """Add a local-test vehicle; unit_of_length is the site's display preference."""
    if unit_of_length not in {"km", "mi"}:
        raise ValueError("unit_of_length must be km or mi")
    await db.execute("UPDATE settings SET unit_of_length=$1", unit_of_length)
    settings_id = await db.fetchval("INSERT INTO car_settings DEFAULT VALUES RETURNING id")
    return await db.fetchval(
        """INSERT INTO cars(eid,vid,vin,name,model,efficiency,settings_id,inserted_at,updated_at)
        SELECT n,n,'FIXTURE'||lpad(n::text,10,'0'),$1,'3',$2,$3,$4,$4
        FROM (SELECT coalesce(max(id),0)+1 AS n FROM cars) next_id RETURNING id""",
        name,
        efficiency,
        settings_id,
        datetime(2026, 1, 1),
    )


async def seed(db: asyncpg.Connection) -> None:
    await db.execute("""
        INSERT INTO settings(id,unit_of_length,preferred_range,inserted_at,updated_at)
        VALUES (1,'km','rated','2026-01-01','2026-01-01') ON CONFLICT DO NOTHING;
        INSERT INTO car_settings(id) VALUES (1),(2) ON CONFLICT DO NOTHING;
        INSERT INTO cars(id,eid,vid,vin,name,model,efficiency,settings_id,inserted_at,updated_at)
        VALUES (1,1001,2001,'FIXTURE00000000001','行笺测试车','3',0.15,1,'2026-01-01','2026-01-01'),
               (2,1002,2002,'FIXTURE00000000002',NULL,'Y',NULL,2,'2026-01-01','2026-01-01')
        ON CONFLICT DO NOTHING;
        INSERT INTO states(id,car_id,state,start_date,end_date) VALUES
          (1,1,'online','2026-03-14 00:00','2026-03-14 12:00'),
          (2,1,'asleep','2026-03-14 12:00','2026-03-14 22:00'),
          (3,1,'offline','2026-03-14 22:00','2026-03-14 23:00'),
          (4,1,'online','2026-03-14 23:00',NULL)
        ON CONFLICT DO NOTHING;
    """)
