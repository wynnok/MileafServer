"""Charging sessions spanning January-March, with real v4.3 phase constraints."""

from datetime import datetime, timedelta
from decimal import Decimal

import asyncpg


async def seed(db: asyncpg.Connection) -> None:
    dates = [
        datetime(2026, 1, 5, 8),
        datetime(2026, 2, 5, 8),
        datetime(2026, 2, 15, 8),
        datetime(2026, 3, 1, 8),
        datetime(2026, 3, 2, 8),
        datetime(2026, 3, 3, 8),
        datetime(2026, 3, 4, 8),
        datetime(2026, 3, 15, 11),
        datetime(2026, 3, 29, 1),
    ]
    for session_id, start in enumerate(dates, 1):
        ongoing = session_id == 8
        end = None if ongoing else start + timedelta(hours=1)
        cost = None if session_id == 3 else Decimal(0 if session_id == 4 else 12)
        await db.execute(
            """INSERT INTO charging_processes(id,car_id,position_id,start_date,end_date,
                charge_energy_added,charge_energy_used,start_battery_level,end_battery_level,
                start_rated_range_km,end_rated_range_km,start_ideal_range_km,end_ideal_range_km,
                duration_min,outside_temp_avg,cost,geofence_id,address_id)
            VALUES ($1,1,999999,$2,$3,20,22,40,70,200,350,220,380,$4,15,$5,$6,$7)
            ON CONFLICT DO NOTHING""",
            session_id,
            start,
            end,
            None if ongoing else 60,
            cost,
            1 if session_id == 1 else None,
            1 if session_id == 1 else 2,
        )
        count = 0 if session_id == 5 else 2 if session_id == 6 else 61
        for index in range(count):
            # In v4.3 charger_phases <= 0 is normalized to NULL by upstream migrations.
            phases = None if session_id == 2 else 3
            if session_id == 7:
                phases = (None, 1, 3)[index % 3]
            sample_end = datetime(2026, 3, 15, 11, 50) if ongoing else end
            assert sample_end is not None
            date = start + (sample_end - start) * index / (count - 1)
            await db.execute(
                """INSERT INTO charges(id,charging_process_id,date,charge_energy_added,
                    charger_power,charger_phases,charger_voltage,charger_actual_current,
                    battery_level,ideal_battery_range_km,rated_battery_range_km,
                    fast_charger_present,fast_charger_type,outside_temp)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,300,280,$10,$11,15)
                ON CONFLICT DO NOTHING""",
                session_id * 1000 + index,
                session_id,
                date,
                Decimal(20) * index / (count - 1),
                120 if session_id == 2 else 11,
                phases,
                400 if session_id == 2 else 230,
                300 if session_id == 2 else 16,
                40 + int(30 * index / (count - 1)),
                None if session_id == 7 else session_id == 2,
                "TeslaSupercharger" if session_id == 2 else None,
            )
