"""Read-only charging sessions with added and grid-side energy kept distinct."""

from datetime import datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from mileaf.cache import TTLCache, cached_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_cache, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.probes import CAPABILITIES
from mileaf.queries.charging import is_dc_sql
from mileaf.queries.locations import Location, LocationRef, fetch_locations
from mileaf.queries.pagination import fetch_page
from mileaf.queries.parameters import ListParameters, ResourceID, ResourceRoute, list_parameters
from mileaf.queries.series import fetch_series
from mileaf.units import UnitSettings, get_units
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1", tags=["charging sessions"], route_class=ResourceRoute)


class ChargingSession(BaseModel):
    id: int
    started_at: datetime
    ended_at: datetime | None
    duration_min: int | None
    energy_added_kwh: float | None = Field(description="Energy added to the battery, kWh")
    energy_used_kwh: float | None = Field(description="Grid-side energy consumed, kWh")
    start_soc: int | None
    end_soc: int | None
    cost: float | None
    is_dc: bool | None
    location: Location
    is_active: bool


class ChargingSessionDetail(ChargingSession):
    vehicle_id: int
    outside_temp_c_avg: float | None
    start_battery_level: int | None
    end_battery_level: int | None
    charger_phases_mode: int | None


SESSION_SOURCE = f"""
    SELECT cp.*, cp.car_id AS vehicle_id,
        cp.start_date AT TIME ZONE 'UTC' AS started_at,
        cp.end_date AT TIME ZONE 'UTC' AS ended_at,
        /* TeslaMate fills the process aggregate on completion; an active session keeps
           its current cumulative value in the latest charge sample. */
        CASE WHEN cp.end_date IS NULL THEN coalesce(
            (SELECT c.charge_energy_added FROM charges c
             WHERE c.charging_process_id=cp.id
             ORDER BY c.date DESC,c.id DESC LIMIT 1), cp.charge_energy_added)
        ELSE cp.charge_energy_added END AS energy_added_kwh,
        {is_dc_sql()} AS is_dc,
        (SELECT mode() WITHIN GROUP (ORDER BY charger_phases)
         FROM charges WHERE charging_process_id=cp.id) AS charger_phases_mode
    FROM charging_processes cp JOIN cars c ON c.id=cp.car_id
"""


async def session_models(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    rows: list[asyncpg.Record],
) -> list[ChargingSessionDetail]:
    locations = await fetch_locations(
        db, [LocationRef(row["position_id"], row["address_id"], row["geofence_id"]) for row in rows]
    )
    result = []
    for row, location in zip(rows, locations, strict=True):
        active = row["ended_at"] is None
        result.append(
            ChargingSessionDetail(
                id=row["id"],
                vehicle_id=row["vehicle_id"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                duration_min=None if active else row["duration_min"],
                energy_added_kwh=row["energy_added_kwh"],
                energy_used_kwh=row["charge_energy_used"],
                start_soc=row["start_battery_level"],
                end_soc=None if active else row["end_battery_level"],
                cost=row["cost"],
                is_dc=row["is_dc"],
                location=location,
                is_active=active,
                outside_temp_c_avg=row["outside_temp_avg"],
                start_battery_level=row["start_battery_level"],
                end_battery_level=None if active else row["end_battery_level"],
                charger_phases_mode=row["charger_phases_mode"],
            )
        )
    return result


@router.get("/charging-sessions/{session_id}", response_model=ChargingSessionDetail)
async def charging_session_detail(
    session_id: ResourceID,
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
    cache: Annotated[TTLCache, Depends(get_cache)],
) -> Response:
    async def render() -> bytes:
        async with pool.acquire() as db:
            rows = await fetch_rows(db, SESSION_SOURCE + " WHERE cp.id=$1", session_id)
            if not rows:
                raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
            return (await session_models(db, rows))[0].model_dump_json().encode()

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


class ChargingSessionPage(BaseModel):
    items: list[ChargingSession]
    next_cursor: str | None


@router.get("/vehicles/{vehicle_id}/charging-sessions", response_model=ChargingSessionPage)
async def charging_session_list(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    params: Annotated[ListParameters, Depends(list_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    cache: Annotated[TTLCache, Depends(get_cache)],
) -> Response:
    async def render() -> bytes:
        async with pool.acquire() as db:
            page = await fetch_page(
                db,
                source=SESSION_SOURCE + " WHERE cp.car_id=$1",
                args=(vehicle.id,),
                from_=params.from_,
                to=params.to,
                cursor=params.cursor,
                limit=params.limit,
            )
            items = await session_models(db, page.rows)
            return (
                ChargingSessionPage(items=items, next_cursor=page.next_cursor)
                .model_dump_json()
                .encode()
            )

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


class ChargePoint(BaseModel):
    t: datetime
    power: float | None = None
    soc: int | None = None


class ChargingSessionSeries(BaseModel):
    session_id: int
    resolution: Literal["empty", "server_sampled"]
    points: int
    series: list[ChargePoint]


@router.get(
    "/charging-sessions/{session_id}/series",
    response_model=ChargingSessionSeries,
    response_model_exclude_unset=True,
)
async def charging_session_series(
    session_id: ResourceID,
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
    cache: Annotated[TTLCache, Depends(get_cache)],
    fields: str = "power,soc",
    points: str = "200",
) -> Response:
    async def render() -> bytes:
        async with pool.acquire() as db:
            exists = await fetch_rows(
                db,
                "SELECT cp.id FROM charging_processes cp JOIN cars c ON c.id=cp.car_id "
                "WHERE cp.id=$1",
                session_id,
            )
            if not exists:
                raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
            sampled = await fetch_series(
                db,
                table="charges",
                time_column="date",
                columns={"power": "charger_power", "soc": "battery_level"},
                where="charging_process_id=$1",
                args=(session_id,),
                fields=fields,
                points=points,
            )
        return (
            ChargingSessionSeries(
                session_id=session_id,
                resolution=sampled.resolution,
                points=len(sampled.series),
                series=[ChargePoint(**row) for row in sampled.series],
            )
            .model_dump_json(exclude_unset=True)
            .encode()
        )

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


CAPABILITIES["charging_session_details"] = {
    "charging_processes": {
        **{name: ("int4",) for name in ("position_id", "address_id", "geofence_id")},
        **{
            name: ("int2",) for name in ("duration_min", "start_battery_level", "end_battery_level")
        },
        **{name: ("numeric",) for name in ("charge_energy_used", "outside_temp_avg", "cost")},
    },
    "charges": {
        "id": ("int4",),
        "date": ("timestamp",),
        "charge_energy_added": ("numeric",),
        "charger_power": ("int2",),
        "battery_level": ("int2",),
    },
}
