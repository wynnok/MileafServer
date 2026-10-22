"""Fifty drives, explicit boundaries and dense/sparse/missing position series."""

from datetime import datetime, timedelta
from decimal import Decimal

import asyncpg

# IDs are stable so later tickets can select a scenario without editing this seed.
BOUNDARIES = {
    1: ("2026-03-15T08:00", "2026-03-15T10:00"),  # 121 one-minute positions
    2: ("2026-03-15T10:30", "2026-03-15T10:32"),  # three positions
    3: ("2026-03-14T09:00", "2026-03-14T09:30"),
    4: ("2026-03-05T09:00", "2026-03-05T09:30"),
    5: ("2026-02-15T09:00", "2026-02-15T09:30"),
    6: ("2026-03-14T23:30", "2026-03-15T00:40"),
    7: ("2026-03-15T11:00", None),
    8: ("2026-03-03T09:00", "2026-03-03T09:30"),  # non-positive range difference
    9: ("2026-03-02T09:00", "2026-03-02T09:30"),  # zero distance
    10: ("2026-02-10T09:00", "2026-02-10T09:30"),  # geofence endpoints
    11: ("2026-02-11T09:00", "2026-02-11T09:30"),  # address endpoints
    12: ("2026-02-12T09:00", "2026-02-12T09:30"),  # coordinates only
    13: ("2026-02-13T09:00", "2026-02-13T09:30"),  # no locations or positions
    14: ("2026-03-28T22:30", "2026-03-28T23:00"),
    15: ("2026-03-29T00:30", "2026-03-29T01:00"),
    16: ("2026-03-29T21:30", "2026-03-29T22:00"),
    17: ("2026-03-30T02:00", "2026-03-30T02:30"),
}


async def seed(db: asyncpg.Connection) -> None:
    for drive_id in range(1, 51):
        if drive_id in BOUNDARIES:
            start_text, end_text = BOUNDARIES[drive_id]
            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text) if end_text else None
        else:
            start = datetime(2026, 1, 1, 9) + timedelta(days=drive_id - 18)
            end = start + timedelta(minutes=30)
        await db.execute(
            """INSERT INTO drives(id,car_id,start_date,end_date,distance,duration_min,
                start_km,end_km,start_rated_range_km,end_rated_range_km,
                start_ideal_range_km,end_ideal_range_km,outside_temp_avg,inside_temp_avg,
                speed_max,power_max,power_min,ascent,descent)
            VALUES ($1,1,$2,$3,$4,$5,10000,10020,350,$6,380,$6,15,21,90,120,-35,40,30)
            ON CONFLICT DO NOTHING""",
            drive_id,
            start,
            end,
            0.0 if drive_id == 9 else 20.0,
            int((end - start).total_seconds() / 60) if end else None,
            Decimal(350 if drive_id == 8 else 325),
        )
        if drive_id in {13, 14, 15, 16, 17}:
            continue
        count = 121 if drive_id == 1 else 3 if drive_id == 2 else 2
        position_ids = []
        for index in range(count):
            position_id = drive_id * 1000 + index
            position_ids.append(position_id)
            sample_end = end or datetime(2026, 3, 15, 11, 30)
            date = start + (sample_end - start) * index / (count - 1)
            await db.execute(
                """INSERT INTO positions(id,car_id,drive_id,date,latitude,longitude,elevation,
                    speed,power,odometer,battery_level,rated_battery_range_km,
                    ideal_battery_range_km,est_battery_range_km,outside_temp,inside_temp)
                VALUES ($1,1,$2,$3,31.23,121.47,20,60,15,$4,70,325,355,310,15,21)
                ON CONFLICT DO NOTHING""",
                position_id,
                drive_id,
                date,
                10000.0 + index * 20 / (count - 1),
            )
        await db.execute(
            "UPDATE drives SET start_position_id=$2,end_position_id=$3 WHERE id=$1",
            drive_id,
            position_ids[0],
            position_ids[-1] if end else None,
        )
    await db.execute("""
        UPDATE drives SET start_geofence_id=1,end_geofence_id=2 WHERE id=10;
        UPDATE drives SET start_address_id=1,end_address_id=2 WHERE id=11;
        INSERT INTO positions(id,car_id,date,latitude,longitude,elevation,battery_level,
            rated_battery_range_km,ideal_battery_range_km,est_battery_range_km,
            outside_temp,inside_temp,odometer,speed,power)
        VALUES (999999,1,'2026-03-15 11:55',31.2304,121.4737,42,68,320,350,300,16,22,10020,0,0)
        ON CONFLICT DO NOTHING
    """)
