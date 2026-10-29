"""Time-weighted speed distribution over real, bounded sample intervals."""

from datetime import datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Request, Response

from mileaf.database import fetch_rows
from mileaf.dependencies import get_now, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.owner_history import OwnerResult, cached_owner_response, respond
from mileaf.owner_models import SpeedBin
from mileaf.queries.parameters import ResourceID, ResourceRoute
from mileaf.units import UnitSettings, get_units

router = APIRouter(prefix="/v1/drives", route_class=ResourceRoute)


@router.get("/{drive_id}/speed-histogram", response_model=OwnerResult[SpeedBin])
async def speed_histogram(
    drive_id: ResourceID,
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
    now: Annotated[datetime, Depends(get_now)],
) -> Response:
    async with pool.acquire() as db:
        drives = await fetch_rows(
            db,
            "SELECT d.start_date AT TIME ZONE 'UTC' AS start,"
            "d.end_date AT TIME ZONE 'UTC' AS end FROM drives d JOIN cars c ON c.id=d.car_id "
            "WHERE d.id=$1",
            drive_id,
        )
        if not drives:
            raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
        cached = cached_owner_response(request)
        if cached is not None:
            return cached
        start, end = drives[0]["start"], min(drives[0]["end"] or now, now)
        rows = await fetch_rows(
            db,
            "WITH source AS (SELECT DISTINCT ON (date) date,speed FROM positions WHERE drive_id=$1 "
            "AND date >= $2 AT TIME ZONE 'UTC' AND date <= $3 AT TIME ZONE 'UTC' "
            "ORDER BY date,id DESC), intervals AS (SELECT *,extract(epoch FROM "
            "(lead(date) OVER (ORDER BY date)-date)) AS dt FROM source) "
            "SELECT least(4,floor(speed/30.0))::int AS bin,sum(dt) AS seconds,"
            "count(*) AS valid_intervals FROM intervals WHERE dt>0 AND dt<=300 AND speed>=0 "
            "GROUP BY 1",
            drive_id,
            start,
            end,
        )
    counts = {r["bin"]: float(r["seconds"]) for r in rows}
    observed = sum(counts.values())
    span = max(0, (end - start).total_seconds())
    body: OwnerResult = OwnerResult(
        query={"drive_id": drive_id},
        as_of=now,
        method="speed-previous-hold-v1",
        status="available" if observed else "insufficient_data",
        items=[
            {
                "from_kmh": i * 30,
                "to_kmh": (i + 1) * 30 if i < 4 else None,
                "seconds": counts.get(i, 0),
            }
            for i in range(5)
        ],
        totals={"observed_seconds": observed, "uncovered_seconds": max(0, span - observed)},
        coverage={"candidate_seconds": span, "observed_seconds": observed},
    )
    return await respond(request, body)
