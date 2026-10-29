"""Recorded firmware updates with a complete-set predecessor independent of pages."""

from datetime import UTC, datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Request, Response

from mileaf.database import fetch_rows
from mileaf.dependencies import get_now, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.owner_history import OwnerResult, binding, cached_owner_response, respond, supported
from mileaf.owner_models import FirmwareUpdate
from mileaf.queries.charging_query import decode_bound_cursor, encode_bound_cursor
from mileaf.queries.pagination import bounded_integer
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1/vehicles/{vehicle_id}", tags=["owner"])


@router.get("/software-updates", response_model=OwnerResult[FirmwareUpdate])
async def software_updates(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    now: Annotated[datetime, Depends(get_now)],
    limit: str = "20",
    cursor: str | None = None,
) -> Response:
    size = bounded_integer(limit, default_name="limit", minimum=1, maximum=100)
    identity = binding(request, vehicle.id, "software-updates-v1", None)
    key = decode_bound_cursor(cursor, identity)
    time, identifier = None, None
    if key is not None:
        try:
            if len(key) != 2 or type(key[1]) is not int or not 0 < key[1] <= 2147483647:
                raise ValueError
            time = datetime.fromisoformat(key[0])
            if time.tzinfo is None:
                raise ValueError
            time, identifier = time.astimezone(UTC), key[1]
        except (ValueError, TypeError, OverflowError):
            raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid update cursor") from None
    body: OwnerResult = OwnerResult(
        query={"vehicle_id": vehicle.id},
        as_of=now,
        method="software-updates-v1",
        status="unsupported",
    )
    if not supported(request, "software_updates"):
        return await respond(request, body)
    cached = cached_owner_response(request)
    if cached is not None:
        return cached
    async with pool.acquire() as db:
        rows = await fetch_rows(
            db,
            "WITH matched AS (SELECT *,start_date AT TIME ZONE 'UTC' AS started_at,"
            "end_date AT TIME ZONE 'UTC' AS ended_at FROM updates WHERE car_id=$1 "
            "AND start_date <= $4 AT TIME ZONE 'UTC'), page AS (SELECT * FROM matched "
            "WHERE ($2::timestamptz IS NULL OR (started_at,id)<($2,$3::int)) "
            "ORDER BY started_at DESC,id DESC LIMIT $5) SELECT p.*,"
            "(SELECT max(ended_at) FROM matched m WHERE m.ended_at<p.started_at) "
            "AS previous_completed_at,(SELECT max(started_at) FROM matched) AS last "
            "FROM page p ORDER BY started_at DESC,id DESC",
            vehicle.id,
            time,
            identifier,
            now,
            size + 1,
        )
    items = []
    for row in rows[:size]:
        prev = row["previous_completed_at"]
        items.append(
            {
                "id": row["id"],
                "version": row["version"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "status": "completed" if row["ended_at"] else "ongoing",
                "previous_completed_at": prev,
                "interval_seconds": (row["started_at"] - prev).total_seconds() if prev else None,
                "coverage": {"predecessor_available": prev is not None},
            }
        )
    body.items = items
    body.status = "available" if items else "no_records"
    body.source_last_at = rows[0]["last"] if rows else None
    if len(rows) > size:
        row = rows[size - 1]
        body.next_cursor = encode_bound_cursor(identity, [row["started_at"].isoformat(), row["id"]])
    return await respond(request, body)
