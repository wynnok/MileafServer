"""Read-only drive detail and metric projection."""

import base64
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
from mileaf.queries.driving_query import (
    DRIVING_MATCH_METHOD,
    DrivingQuery,
    DrivingQueryEcho,
    drive_cursor_key,
    driving_parameters,
    encode_drive_cursor,
    match_source,
)
from mileaf.queries.energy import consumption_wh_km_sql, energy_kwh_sql
from mileaf.queries.locations import Location, LocationRef, fetch_locations
from mileaf.queries.owner_fields import POSITION_FIELDS
from mileaf.queries.pagination import decode_cursor, fetch_page
from mileaf.queries.parameters import ResourceID, ResourceRoute
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
    ascent_m: float | None = None
    descent_m: float | None = None
    elevation_quality: dict[str, str] = {}
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
           duration_min takes precedence; zero or NULL elapsed time is the fallback. */
        d.distance / nullif(coalesce(nullif(d.duration_min, 0),
            extract(epoch FROM (d.end_date-d.start_date))/60),0) * 60 AS speed_avg_kmh,
        sp.battery_level AS start_battery_level, ep.battery_level AS end_battery_level
    FROM drives d JOIN cars c ON c.id=d.car_id CROSS JOIN settings s
    LEFT JOIN positions sp ON sp.id=d.start_position_id
    LEFT JOIN positions ep ON ep.id=d.end_position_id
"""


async def drive_models(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    rows: list[asyncpg.Record],
    columns: frozenset[tuple[str, str]],
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
                ascent_m=row.get("ascent")
                if ("drives", "ascent") in columns
                and row.get("ascent") is not None
                and row["ascent"] >= 0
                else None,
                descent_m=row.get("descent")
                if ("drives", "descent") in columns
                and row.get("descent") is not None
                and row["descent"] >= 0
                else None,
                elevation_quality={
                    name: "unsupported"
                    if ("drives", name) not in columns
                    else "available"
                    if row[name] is not None and row[name] >= 0
                    else "insufficient_data"
                    for name in ("ascent", "descent")
                },
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
            return (
                (await drive_models(db, rows, request.app.state.compatibility.owner_columns))[0]
                .model_dump_json()
                .encode()
            )

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


class DrivePage(BaseModel):
    items: list[Drive]
    next_cursor: str | None
    query: DrivingQueryEcho
    as_of: datetime
    matched_drives: int
    method: Literal["driving-match-v1"] = DRIVING_MATCH_METHOD
    version: int = 1
    thresholds: dict[str, float] = {}
    status: Literal["available", "no_records"]


@router.get("/vehicles/{vehicle_id}/drives", response_model=DrivePage)
async def drive_list(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    params: Annotated[DrivingQuery, Depends(driving_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    cache: Annotated[TTLCache, Depends(get_cache)],
) -> Response:
    key = drive_cursor_key(params)

    async def render() -> bytes:
        async with pool.acquire() as db:
            source, args = match_source(params.echo, vehicle.id, source=DRIVE_SOURCE)
            count = await fetch_rows(db, f"SELECT count(*) AS count FROM ({source}) matched", *args)
            # fetch_page accepts the original time/id key; expose only query-bound cursors.
            legacy_cursor = None
            if key is not None:
                legacy_cursor = (
                    base64.urlsafe_b64encode(f"{key[0].isoformat()}|{key[1]}".encode())
                    .decode()
                    .rstrip("=")
                )
            page = await fetch_page(
                db,
                source=source,
                args=tuple(args),
                cursor=legacy_cursor,
                limit=params.limit,
            )
            items = await drive_models(db, page.rows, request.app.state.compatibility.owner_columns)
            next_cursor = None
            if page.next_cursor is not None:
                timestamp, identifier = decode_cursor(page.next_cursor)
                next_cursor = encode_drive_cursor(params, timestamp, identifier)
            return (
                DrivePage(
                    items=items,
                    next_cursor=next_cursor,
                    query=params.echo,
                    as_of=params.as_of,
                    matched_drives=count[0]["count"],
                    status="available" if count[0]["count"] else "no_records",
                )
                .model_dump_json(by_alias=True)
                .encode()
            )

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
    power_kw: float | None = None
    inside_temp_c: float | None = None
    outside_temp_c: float | None = None
    tpms_fl_bar: float | None = None
    tpms_fr_bar: float | None = None
    tpms_rl_bar: float | None = None
    tpms_rr_bar: float | None = None


class DriveSeries(BaseModel):
    drive_id: int
    resolution: Literal["empty", "server_sampled"]
    points: int
    series: list[DrivePoint]
    field_support: dict[str, bool] = {}


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
    extra = {
        "power": "power_kw",
        "inside_temperature": "inside_temp_c",
        "outside_temperature": "outside_temp_c",
        **{f"tpms_{wheel}": f"tpms_{wheel}_bar" for wheel in ("fl", "fr", "rl", "rr")},
    }
    available = request.app.state.compatibility.owner_columns
    support = {
        name: ("positions", POSITION_FIELDS[output][0]) in available
        for name, output in extra.items()
    }
    if any(field not in {"speed", "battery", "elevation", *extra} for field in fields.split(",")):
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
                    **{
                        name: POSITION_FIELDS[output][0] if support[name] else "NULL::numeric"
                        for name, output in extra.items()
                    },
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
        names = {
            "speed": "speed_kmh",
            "battery": "battery_level",
            "elevation": "elevation_m",
            **extra,
        }
        result = []
        for point in sampled.series:
            data = {names.get(key, key): value for key, value in point.items()}
            if "speed_kmh" in data:
                data["speed_kmh"] = speed_kmh(data["speed_kmh"])
            result.append(DrivePoint(**data))
        return (
            DriveSeries(
                drive_id=drive_id,
                resolution=sampled.resolution,
                points=len(result),
                series=result,
                field_support={name: support.get(name, True) for name in fields.split(",")},
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
