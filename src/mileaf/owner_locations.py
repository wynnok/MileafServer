"""Completed-drive destinations and bounded angular grid aggregation."""

import math
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Request, Response

from mileaf.database import fetch_rows
from mileaf.dependencies import get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.owner_history import (
    OwnerQuery,
    OwnerResult,
    binding,
    cached_owner_response,
    owner_parameters,
    respond,
    result,
    supported,
)
from mileaf.owner_models import DestinationLocation, GridCell
from mileaf.queries.charging_query import decode_bound_cursor, encode_bound_cursor
from mileaf.queries.locations import location_sql
from mileaf.queries.pagination import bounded_integer
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

# Same identity scheme as charging locations; current destination associations own identity.
DESTINATION_ID = (
    "rtrim(encode(convert_to(CASE WHEN d.end_geofence_id IS NOT NULL THEN "
    "'geofence:'||d.end_geofence_id WHEN d.end_address_id IS NOT NULL THEN "
    "'address:'||d.end_address_id ELSE 'unlocated' END,'UTF8'),'base64'),'=')"
)
MATCH = (
    "d.car_id=$1 AND d.end_date IS NOT NULL "
    "AND d.start_date >= $2 AT TIME ZONE 'UTC' AND d.start_date < $3 AT TIME ZONE 'UTC'"
)


router = APIRouter(prefix="/v1/vehicles/{vehicle_id}", tags=["owner"])


@router.get("/driving-locations", response_model=OwnerResult[DestinationLocation])
async def driving_locations(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    query: Annotated[OwnerQuery, Depends(owner_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    sort: str = "visits_desc",
    limit: str = "20",
    cursor: str | None = None,
) -> Response:
    size = bounded_integer(limit, default_name="limit", minimum=1, maximum=100)
    if sort != "visits_desc":
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid location sort")
    identity = binding(request, vehicle.id, "driving-locations-v1", query)
    key = decode_bound_cursor(cursor, identity)
    count, location = None, None
    if key is not None:
        if len(key) != 2 or type(key[0]) is not int or key[0] < 1 or not isinstance(key[1], str):
            raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid destination cursor")
        count, location = key
    if not supported(request, "driving_locations"):
        return await respond(request, result(query, "driving-locations-v1", status="unsupported"))
    cached = cached_owner_response(request)
    if cached is not None:
        return cached
    source = (
        f"WITH matched AS (SELECT d.*,{DESTINATION_ID} AS location_id FROM drives d WHERE "
        f"{MATCH}), grouped AS (SELECT location_id,count(*) AS visits,"
        "sum(distance) FILTER (WHERE distance>=0 AND distance<'Infinity'::float8) AS distance_km,"
        "count(distance) FILTER (WHERE distance>=0 AND distance<'Infinity'::float8) "
        "AS distance_valid,"
        "sum(duration_min) FILTER (WHERE duration_min>=0) AS duration_min,"
        "count(duration_min) FILTER (WHERE duration_min>=0) AS duration_valid,"
        "max(end_date) AT TIME ZONE 'UTC' AS last_visited_at,"
        "(array_agg(id ORDER BY end_date DESC,id DESC))[1] AS representative_drive_id "
        "FROM matched GROUP BY location_id), ranked AS (SELECT *,row_number() OVER "
        "(ORDER BY visits DESC,location_id) AS rank FROM grouped) "
    )
    async with pool.acquire() as db:
        totals = await fetch_rows(
            db,
            source + "SELECT count(*) AS total_locations,"
            "coalesce(sum(visits) FILTER (WHERE location_id='dW5sb2NhdGVk'),0) AS unlocated_drives,"
            "max(last_visited_at) AS last FROM ranked",
            vehicle.id,
            query.start,
            query.end,
        )
        rows = await fetch_rows(
            db,
            source
            + "SELECT r.*,"
            + location_sql()
            + " AS location FROM ranked r JOIN drives d ON d.id=r.representative_drive_id "
            "LEFT JOIN positions p ON p.id=d.end_position_id "
            "LEFT JOIN geofences g ON g.id=d.end_geofence_id "
            "LEFT JOIN addresses a ON a.id=d.end_address_id "
            "WHERE ($4::bigint IS NULL OR r.visits<$4 OR (r.visits=$4 AND r.location_id>$5::text)) "
            "ORDER BY r.visits DESC,r.location_id LIMIT $6",
            vehicle.id,
            query.start,
            query.end,
            count,
            location,
            size + 1,
        )
    import json

    items = []
    for row in rows[:size]:
        data = dict(row)
        data["location"] = json.loads(data["location"])
        data["coordinate_source"] = "latest_destination_position"
        data["coverage"] = {
            name: {"candidate": row["visits"], "valid": row[name + "_valid"]}
            for name in ("distance", "duration")
        }
        items.append(data)
    next_cursor = None
    if len(rows) > size:
        row = rows[size - 1]
        next_cursor = encode_bound_cursor(identity, [row["visits"], row["location_id"]])
    total = dict(totals[0])
    last = total.pop("last")
    return await respond(
        request,
        result(
            query,
            "driving-locations-v1",
            items=items,
            status="available" if total["total_locations"] else "no_records",
            totals=total,
            next_cursor=next_cursor,
            source_last_at=last,
        ),
    )


@router.get("/visited-areas", response_model=OwnerResult[GridCell])
async def visited_areas(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    query: Annotated[OwnerQuery, Depends(owner_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    zoom: str,
    bbox: str,
) -> Response:
    level = bounded_integer(zoom, default_name="zoom", minimum=0, maximum=16)
    try:
        west, south, east, north = map(float, bbox.split(","))
        if not all(math.isfinite(x) for x in (west, south, east, north)) or not (
            -180 <= west < east <= 180 and -90 <= south < north <= 90
        ):
            raise ValueError
    except ValueError:
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid bbox") from None
    if not supported(request, "visited_areas"):
        return await respond(request, result(query, "destination-grid-v1", status="unsupported"))
    cached = cached_owner_response(request)
    if cached is not None:
        return cached
    width = 360 / 2**level
    async with pool.acquire() as db:
        rows = await fetch_rows(
            db,
            "WITH matched AS (SELECT p.latitude AS lat,p.longitude AS lon "
            "FROM drives d LEFT JOIN positions p ON p.id=d.end_position_id WHERE "
            + MATCH
            + "), grouped AS (SELECT floor((lon+180)/$4)::int AS x,floor((lat+90)/$4)::int AS y,"
            "count(*) AS count FROM matched WHERE lon>=$5 AND lon<$6 AND lat>=$7 AND lat<$8 "
            "GROUP BY 1,2) SELECT *,count(*) OVER () AS total_cells FROM grouped "
            "ORDER BY count DESC,x,y LIMIT 1000",
            vehicle.id,
            query.start,
            query.end,
            width,
            west,
            east,
            south,
            north,
        )
        totals = await fetch_rows(
            db,
            "SELECT count(*) FILTER (WHERE p.id IS NULL) AS unlocated_drives,"
            "max(d.end_date) AT TIME ZONE 'UTC' AS last FROM drives d "
            "LEFT JOIN positions p ON p.id=d.end_position_id WHERE " + MATCH,
            vehicle.id,
            query.start,
            query.end,
        )
    items = [
        {
            "count": row["count"],
            "bounds": [
                row["x"] * width - 180,
                row["y"] * width - 90,
                min(180, (row["x"] + 1) * width - 180),
                min(90, (row["y"] + 1) * width - 90),
            ],
            "center": {
                "lon": (row["x"] + 0.5) * width - 180,
                "lat": (row["y"] * width - 90 + min(90, (row["y"] + 1) * width - 90)) / 2,
            },
        }
        for row in rows
    ]
    cells = rows[0]["total_cells"] if rows else 0
    return await respond(
        request,
        result(
            query,
            "destination-grid-v1",
            items=items,
            status="available" if items else "no_records",
            source_last_at=totals[0]["last"],
            totals={
                "total_cells": cells,
                "truncated": cells > 1000,
                "grid_degrees": width,
                "zoom": level,
                "bbox": [west, south, east, north],
                "unlocated_drives": totals[0]["unlocated_drives"],
            },
        ),
    )
