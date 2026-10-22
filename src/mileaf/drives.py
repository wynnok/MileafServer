"""Read-only drive detail and metric projection."""

from datetime import datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.responses import Response

from mileaf.cache import TTLCache, cached_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_cache, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.probes import CAPABILITIES
from mileaf.queries.energy import consumption_wh_km_sql, energy_kwh_sql
from mileaf.queries.locations import Location, LocationRef, fetch_locations
from mileaf.queries.pagination import fetch_page
from mileaf.queries.parameters import ListParameters, ResourceID, ResourceRoute, list_parameters
from mileaf.queries.series import fetch_series
from mileaf.units import UnitSettings, consumption_wh_km, distance_km, get_units, speed_kmh
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1", tags=["drives"], route_class=ResourceRoute)


class DriveLocation(Location):
    battery_level: int | None


class Drive(BaseModel):
    id: int
    started_at: datetime
    ended_at: datetime | None
    distance_km: float | None
    duration_min: int | None
    energy_kwh: float | None
    consumption_wh_km: float | None
    start: Location
    end: Location


class DriveDetail(Drive):
    vehicle_id: int
    speed_max_kmh: float | None
    speed_avg_kmh: float | None
    power_max_kw: float | None
    outside_temp_c_avg: float | None
    inside_temp_c_avg: float | None
    range_kind: Literal["rated", "ideal"]
    start: DriveLocation
    end: DriveLocation


DRIVE_SOURCE = f"""
    SELECT d.*, d.car_id AS vehicle_id,
        d.start_date AT TIME ZONE 'UTC' AS started_at,
        d.end_date AT TIME ZONE 'UTC' AS ended_at,
        {energy_kwh_sql()} AS energy_kwh,
        {consumption_wh_km_sql()} AS consumption_wh_km,
        s.preferred_range::text AS range_kind,
        /* TeslaMate v4.3.0 drives: stored km / minutes * 60 -> km/h;
           duration_min takes precedence; elapsed time is the NULL fallback. */
        d.distance / nullif(coalesce(d.duration_min,
            extract(epoch FROM (d.end_date-d.start_date))/60),0) * 60 AS speed_avg_kmh,
        sp.battery_level AS start_battery_level, ep.battery_level AS end_battery_level
    FROM drives d JOIN cars c ON c.id=d.car_id CROSS JOIN settings s
    LEFT JOIN positions sp ON sp.id=d.start_position_id
    LEFT JOIN positions ep ON ep.id=d.end_position_id
"""


async def drive_models(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, rows: list[asyncpg.Record]
) -> list[DriveDetail]:
    refs = [
        LocationRef(row[f"{end}_position_id"], row[f"{end}_address_id"], row[f"{end}_geofence_id"])
        for row in rows
        for end in ("start", "end")
    ]
    locations = await fetch_locations(db, refs)
    result = []
    for index, row in enumerate(rows):
        finished = row["ended_at"] is not None
        result.append(
            DriveDetail(
                id=row["id"],
                vehicle_id=row["vehicle_id"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                distance_km=distance_km(row["distance"]) if finished else None,
                duration_min=row["duration_min"] if finished else None,
                energy_kwh=row["energy_kwh"] if finished else None,
                consumption_wh_km=consumption_wh_km(row["consumption_wh_km"]) if finished else None,
                speed_max_kmh=speed_kmh(row["speed_max"]),
                speed_avg_kmh=speed_kmh(row["speed_avg_kmh"]) if finished else None,
                power_max_kw=row["power_max"],
                outside_temp_c_avg=row["outside_temp_avg"],
                inside_temp_c_avg=row["inside_temp_avg"],
                range_kind=row["range_kind"],
                start=DriveLocation(
                    **locations[2 * index].model_dump(), battery_level=row["start_battery_level"]
                ),
                end=DriveLocation(
                    **locations[2 * index + 1].model_dump(),
                    battery_level=row["end_battery_level"] if finished else None,
                ),
            )
        )
    return result


@router.get("/drives/{drive_id}", response_model=DriveDetail)
async def drive_detail(
    drive_id: ResourceID,
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
    cache: Annotated[TTLCache, Depends(get_cache)],
) -> Response:
    async def render() -> bytes:
        async with pool.acquire() as db:
            rows = await fetch_rows(db, DRIVE_SOURCE + " WHERE d.id=$1", drive_id)
            if not rows:
                raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
            return (await drive_models(db, rows))[0].model_dump_json().encode()

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


class DrivePage(BaseModel):
    items: list[Drive]
    next_cursor: str | None


@router.get("/vehicles/{vehicle_id}/drives", response_model=DrivePage)
async def drive_list(
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
                source=DRIVE_SOURCE + " WHERE d.car_id=$1",
                args=(vehicle.id,),
                from_=params.from_,
                to=params.to,
                cursor=params.cursor,
                limit=params.limit,
            )
            items = await drive_models(db, page.rows)
            return DrivePage(items=items, next_cursor=page.next_cursor).model_dump_json().encode()

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


class DrivePoint(BaseModel):
    t: datetime
    lat: float | None
    lon: float | None
    speed_kmh: float | None = None
    battery_level: int | None = None
    elevation_m: float | None = None


class DriveSeries(BaseModel):
    drive_id: int
    resolution: Literal["empty", "server_sampled"]
    points: int
    series: list[DrivePoint]


@router.get(
    "/drives/{drive_id}/series", response_model=DriveSeries, response_model_exclude_unset=True
)
async def drive_series(
    drive_id: ResourceID,
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
    cache: Annotated[TTLCache, Depends(get_cache)],
    fields: str = "speed",
    points: str = "200",
) -> Response:
    if any(field not in {"speed", "battery", "elevation"} for field in fields.split(",")):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid fields")

    async def render() -> bytes:
        async with pool.acquire() as db:
            exists = await fetch_rows(
                db, "SELECT d.id FROM drives d JOIN cars c ON c.id=d.car_id WHERE d.id=$1", drive_id
            )
            if not exists:
                raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
            sampled = await fetch_series(
                db,
                table="positions",
                time_column="date",
                columns={
                    "speed": "speed",
                    "battery": "battery_level",
                    "elevation": "elevation",
                    "lat": "latitude",
                    "lon": "longitude",
                },
                where="drive_id=$1",
                args=(drive_id,),
                fields=fields,
                required_fields=("lat", "lon"),
                points=points,
            )
        names = {"speed": "speed_kmh", "battery": "battery_level", "elevation": "elevation_m"}
        result = []
        for point in sampled.series:
            data = {names.get(key, key): value for key, value in point.items()}
            if "speed_kmh" in data:
                data["speed_kmh"] = speed_kmh(data["speed_kmh"])
            result.append(DrivePoint(**data))
        return (
            DriveSeries(
                drive_id=drive_id, resolution=sampled.resolution, points=len(result), series=result
            )
            .model_dump_json(exclude_unset=True)
            .encode()
        )

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


# Register precisely the columns this slice adds to the read-only adapter surface.
CAPABILITIES["drive_details"] = {
    "drives": {
        "id": ("int4",),
        **{
            name: ("int4",)
            for name in (
                "start_position_id",
                "end_position_id",
                "start_address_id",
                "end_address_id",
                "start_geofence_id",
                "end_geofence_id",
            )
        },
        **{name: ("int2",) for name in ("duration_min", "speed_max", "power_max")},
        **{name: ("numeric",) for name in ("outside_temp_avg", "inside_temp_avg")},
    },
    "positions": {
        "drive_id": ("int4",),
        "speed": ("int2",),
        "elevation": ("int2",),
    },
}
