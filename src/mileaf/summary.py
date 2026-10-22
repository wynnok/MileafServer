"""Concurrent overview queries over a vehicle's source data."""

import asyncio
from datetime import datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.responses import Response

from mileaf.cache import TTLCache, cached_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_cache, get_now, get_pool
from mileaf.meta import Freshness
from mileaf.probes import CAPABILITIES
from mileaf.queries.energy import consumption_wh_km_sql, energy_kwh_sql
from mileaf.queries.parameters import ResourceRoute
from mileaf.queries.time import validate_timezone, window_sql
from mileaf.units import consumption_wh_km, distance_km
from mileaf.vehicles import Battery, StateSegment, VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1", tags=["summary"], route_class=ResourceRoute)


class PeriodMetrics(BaseModel):
    drives: int
    distance_km: float
    energy_kwh: float | None


class Periods(BaseModel):
    today: PeriodMetrics
    month: PeriodMetrics


class LastDrive(BaseModel):
    id: int
    started_at: datetime
    distance_km: float | None
    consumption_wh_km: float | None


class LastCharge(BaseModel):
    id: int
    started_at: datetime
    energy_kwh: float | None
    end_soc: int | None


class Firmware(BaseModel):
    version: str | None
    installed_at: datetime


class Summary(BaseModel):
    vehicle: VehicleIdentity
    state: StateSegment | None
    battery: Battery | None
    odometer_km: float | None
    charging_power_kw: float | None
    period: Periods
    last_drive: LastDrive | None
    last_charge: LastCharge | None
    firmware: Firmware | None
    freshness: Freshness


CURRENT_SOURCE = """
    SELECT p.id AS position_id, p.battery_level, p.odometer,
        CASE WHEN s.preferred_range::text='ideal' THEN p.ideal_battery_range_km
             ELSE p.rated_battery_range_km END AS range_km,
        s.preferred_range::text AS range_kind,
        CASE WHEN cp.id IS NOT NULL THEN 'charging' ELSE st.state::text END AS state,
        (CASE WHEN cp.id IS NOT NULL THEN cp.start_date ELSE st.start_date END)
            AT TIME ZONE 'UTC' AS since,
        p.date AT TIME ZONE 'UTC' AS last_position_at,
        st.start_date AT TIME ZONE 'UTC' AS last_state_at,
        (SELECT charger_power FROM charges WHERE charging_process_id=cp.id
         ORDER BY date DESC,id DESC LIMIT 1) AS charging_power_kw
    FROM cars c CROSS JOIN settings s
    LEFT JOIN LATERAL (SELECT * FROM positions WHERE car_id=c.id
        ORDER BY date DESC,id DESC LIMIT 1) p ON true
    LEFT JOIN LATERAL (SELECT state,start_date FROM states WHERE car_id=c.id
        ORDER BY start_date DESC,id DESC LIMIT 1) st ON true
    LEFT JOIN LATERAL (SELECT id,start_date FROM charging_processes
        WHERE car_id=c.id AND end_date IS NULL
        ORDER BY start_date DESC,id DESC LIMIT 1) cp ON true
    WHERE c.id=$1
"""


@router.get("/vehicles/{vehicle_id}/summary", response_model=Summary)
async def summary(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    cache: Annotated[TTLCache, Depends(get_cache)],
    now: Annotated[datetime, Depends(get_now)],
    tz: Annotated[str, Depends(validate_timezone)],
) -> Response:
    async def query(sql: str, *args: object) -> list[asyncpg.Record]:
        # Each segment gets its own bounded pool connection; never share a connection
        # across concurrent asyncpg operations.
        async with pool.acquire() as db:
            return await fetch_rows(db, sql, *args)

    async def render() -> bytes:
        def period_query(period: Literal["today", "month"]) -> str:
            start, end = window_sql(period, now="$2", tz="$3")
            return (
                "/* TeslaMate overview: finished drives by start time in a local calendar "
                "window; sum stored km and preferred-range energy (kWh), unrounded. */ "
                "SELECT count(*) AS drives, coalesce(sum(d.distance),0) AS distance_km, "
                f"CASE WHEN count(*)=0 THEN 0 ELSE sum({energy_kwh_sql()}) END AS energy_kwh "
                "FROM drives d JOIN cars c ON c.id=d.car_id CROSS JOIN settings s "
                "WHERE d.car_id=$1 AND d.end_date IS NOT NULL "
                f"AND d.start_date AT TIME ZONE 'UTC' >= {start} "
                f"AND d.start_date AT TIME ZONE 'UTC' < {end}"
            )

        current, today, month, drives, sessions, firmware = await asyncio.gather(
            query(CURRENT_SOURCE, vehicle.id),
            query(period_query("today"), vehicle.id, now, tz),
            query(period_query("month"), vehicle.id, now, tz),
            query(
                f"SELECT d.id,d.start_date AT TIME ZONE 'UTC' AS started_at, "
                "CASE WHEN d.end_date IS NOT NULL THEN d.distance END AS distance_km, "
                f"CASE WHEN d.end_date IS NOT NULL THEN {consumption_wh_km_sql()} "
                "END AS consumption_wh_km FROM drives d JOIN cars c ON c.id=d.car_id "
                "CROSS JOIN settings s WHERE d.car_id=$1 ORDER BY d.start_date DESC,d.id DESC "
                "LIMIT 1",
                vehicle.id,
            ),
            query(
                "SELECT id,start_date AT TIME ZONE 'UTC' AS started_at, "
                "CASE WHEN end_date IS NULL THEN coalesce("
                "(SELECT c.charge_energy_added FROM charges c "
                "WHERE c.charging_process_id=cp.id ORDER BY c.date DESC,c.id DESC LIMIT 1), "
                "cp.charge_energy_added) ELSE cp.charge_energy_added END AS energy_kwh, "
                "CASE WHEN end_date IS NOT NULL THEN end_battery_level END AS end_soc "
                "FROM charging_processes cp WHERE car_id=$1 "
                "ORDER BY start_date DESC,id DESC LIMIT 1",
                vehicle.id,
            ),
            query(
                "SELECT version,end_date AT TIME ZONE 'UTC' AS installed_at FROM updates "
                "WHERE car_id=$1 AND end_date IS NOT NULL ORDER BY end_date DESC,id DESC LIMIT 1",
                vehicle.id,
            ),
        )
        row = current[0]
        periods = [
            PeriodMetrics(
                drives=rows[0]["drives"],
                distance_km=distance_km(rows[0]["distance_km"]),
                energy_kwh=rows[0]["energy_kwh"],
            )
            for rows in (today, month)
        ]
        last_drive = None
        if drives:
            data = dict(drives[0])
            data["distance_km"] = distance_km(data["distance_km"])
            data["consumption_wh_km"] = consumption_wh_km(data["consumption_wh_km"])
            last_drive = LastDrive(**data)
        result = Summary(
            vehicle=vehicle,
            state=StateSegment(value=row["state"], since=row["since"]) if row["state"] else None,
            battery=Battery(
                level=row["battery_level"],
                range_km=distance_km(row["range_km"]),
                range_kind=row["range_kind"],
            )
            if row["position_id"] is not None
            else None,
            odometer_km=distance_km(row["odometer"]),
            charging_power_kw=row["charging_power_kw"],
            period=Periods(today=periods[0], month=periods[1]),
            last_drive=last_drive,
            last_charge=LastCharge(**dict(sessions[0])) if sessions else None,
            firmware=Firmware(**dict(firmware[0])) if firmware else None,
            freshness=Freshness(
                last_position_at=row["last_position_at"], last_state_at=row["last_state_at"]
            ),
        )
        return result.model_dump_json().encode()

    return await cached_response(
        request,
        cache=cache,
        key=(request.url.path, request.url.query, vehicle.id, tz, int(now.timestamp()) // 30),
        render=render,
        ttl=30,
    )


CAPABILITIES["summary"] = {
    "positions": {"odometer": ("float8",)},
    "charges": {"charge_energy_added": ("numeric",)},
    "updates": {
        "id": ("int4",),
        "car_id": ("int2",),
        "end_date": ("timestamp",),
        "version": ("varchar",),
    },
}
