"""Charging habits: end-time selection, local start grouping, full-set quantiles."""

from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request, Response

from mileaf.database import fetch_rows
from mileaf.dependencies import get_pool
from mileaf.owner_history import (
    OwnerQuery,
    OwnerResult,
    cached_owner_response,
    owner_parameters,
    respond,
    result,
    supported,
)
from mileaf.owner_models import HabitCell
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1/vehicles/{vehicle_id}", tags=["owner"])


@router.get("/charging-analysis", response_model=OwnerResult[HabitCell])
async def charging_habits(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    query: Annotated[OwnerQuery, Depends(owner_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    view: Literal["habits"] = "habits",
) -> Response:
    if not supported(request, "charging_habits"):
        return await respond(request, result(query, "charging-habits-v1", status="unsupported"))
    cached = cached_owner_response(request)
    if cached is not None:
        return cached
    source = (
        "WITH matched AS (SELECT *,start_date AT TIME ZONE 'UTC' AT TIME ZONE $4 AS local "
        "FROM charging_processes WHERE car_id=$1 AND end_date IS NOT NULL "
        "AND end_date >= $2 AT TIME ZONE 'UTC' AND end_date < $3 AT TIME ZONE 'UTC') "
    )
    args = (vehicle.id, query.start, query.end, query.tz)
    async with pool.acquire() as db:
        grid = await fetch_rows(
            db,
            source + "SELECT extract(isodow FROM local)::int AS weekday,"
            "extract(hour FROM local)::int AS hour,"
            "count(*) AS sessions,count(charge_energy_added) FILTER (WHERE charge_energy_added>=0) "
            "AS energy_valid,sum(charge_energy_added) FILTER (WHERE charge_energy_added>=0) "
            "AS energy_added_kwh FROM matched GROUP BY 1,2",
            *args,
        )
        aggregates = await fetch_rows(
            db,
            source + "SELECT count(*) AS sessions,"
            "max(end_date) AT TIME ZONE 'UTC' AS last,"
            + ",".join(
                f"count({column}) FILTER (WHERE {column}>=0) AS {name}_valid,"
                f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {column}) FILTER "
                f"(WHERE {column}>=0) AS {name}_p50,"
                f"percentile_cont(0.9) WITHIN GROUP (ORDER BY {column}) FILTER "
                f"(WHERE {column}>=0) AS {name}_p90"
                for name, column in (
                    ("energy", "charge_energy_added"),
                    ("duration", "duration_min"),
                )
            )
            + " FROM matched",
            *args,
        )
        bins = await fetch_rows(
            db,
            source
            + "SELECT field,least(9,floor(soc/10.0))::int AS bin,count(*) AS count FROM matched "
            "CROSS JOIN LATERAL (VALUES ('start_soc',start_battery_level),"
            "('end_soc',end_battery_level)) v(field,soc) WHERE soc BETWEEN 0 AND 100 GROUP BY 1,2",
            *args,
        )
        ongoing = await fetch_rows(
            db,
            "SELECT count(*) AS count FROM charging_processes "
            "WHERE car_id=$1 AND end_date IS NULL AND start_date >= $2 AT TIME ZONE 'UTC' "
            "AND start_date < $3 AT TIME ZONE 'UTC'",
            *args[:3],
        )
    total = aggregates[0]
    lookup = {(r["weekday"], r["hour"]): dict(r) for r in grid}
    items = [
        lookup.get(
            (day, hour),
            {"weekday": day, "hour": hour, "sessions": 0, "energy_valid": 0, "energy_added_kwh": 0},
        )
        for day in range(1, 8)
        for hour in range(24)
    ]
    for item in items:
        item["coverage"] = {
            "energy_added_kwh": {"candidate": item["sessions"], "valid": item["energy_valid"]}
        }
    totals = {
        "sessions": total["sessions"],
        "ongoing_sessions": ongoing[0]["count"],
        "selection_basis": "ended_at",
        "grouping_basis": "local_started_at",
    }
    for field in ("start_soc", "end_soc"):
        counts = {r["bin"]: r["count"] for r in bins if r["field"] == field}
        totals[field] = [
            {
                "from_pct": i * 10,
                "to_pct": (i + 1) * 10,
                "upper_inclusive": i == 9,
                "count": counts.get(i, 0),
            }
            for i in range(10)
        ]
    coverage = {}
    for name, short in (("energy_added_kwh", "energy"), ("duration_min", "duration")):
        valid = total[short + "_valid"]
        totals[name] = {
            "p50": total[short + "_p50"],
            "p90": total[short + "_p90"],
            "method": "percentile_cont",
            "candidate": total["sessions"],
            "valid": valid,
            "excluded": total["sessions"] - valid,
        }
        coverage[name] = {"candidate": total["sessions"], "valid": valid}
    for field in ("start_soc", "end_soc"):
        valid = sum(r["count"] for r in bins if r["field"] == field)
        coverage[field] = {
            "candidate": total["sessions"],
            "valid": valid,
            "excluded": total["sessions"] - valid,
        }
    return await respond(
        request,
        result(
            query,
            "charging-habits-v1",
            items=items,
            totals=totals,
            coverage=coverage,
            source_last_at=total["last"],
            status="available" if total["sessions"] else "no_records",
        ),
    )
