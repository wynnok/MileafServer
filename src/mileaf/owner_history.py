"""Bounded real position history and intersecting activity events; no interpolation."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from pydantic_core import to_json

from mileaf.cache import cached_response, conditional_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_now, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.owner_models import (
    ActivityEvent,
    BatteryHistoryPoint,
    EnvironmentHistoryPoint,
    LatestObservation,
)
from mileaf.queries.charging_query import decode_bound_cursor, encode_bound_cursor
from mileaf.queries.owner_fields import POSITION_FIELDS, owner_supported
from mileaf.queries.pagination import bounded_integer
from mileaf.queries.parameters import ResourceRoute, list_parameters
from mileaf.queries.series import fetch_series
from mileaf.queries.time import validate_timezone
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1/vehicles/{vehicle_id}", tags=["owner"], route_class=ResourceRoute)


class OwnerResult[T](BaseModel):
    query: dict[str, Any]
    as_of: datetime
    source_last_at: datetime | None = None
    method: str
    version: int = 1
    validation: Literal["synthetic_only"] = "synthetic_only"
    status: Literal["available", "no_records", "insufficient_data", "unsupported"]
    items: list[T] = []
    next_cursor: str | None = None
    coverage: dict[str, Any] = {}
    thresholds: dict[str, Any] = {"provisional": True, "max_gap_seconds": 300}
    raw_points: int | None = None
    deduplicated_points: int | None = None
    output_points: int | None = None
    gaps: dict[str, Any] | None = None
    totals: dict[str, Any] = {}


@dataclass(frozen=True)
class OwnerQuery:
    start: datetime
    end: datetime
    requested_end: datetime
    tz: str
    now: datetime

    def echo(self) -> dict[str, Any]:
        return {"from": self.start, "to": self.end, "tz": self.tz}


def owner_parameters(
    request: Request,
    now: Annotated[datetime, Depends(get_now)],
    from_: Annotated[str, Query(alias="from")],
    to: str,
    tz: str = "UTC",
) -> OwnerQuery:
    bounds = list_parameters(from_, to)
    assert bounds.from_ is not None and bounds.to is not None
    path = request.url.path.rsplit("/", 1)[-1]
    days = (
        31
        if path in {"battery-history", "environment-history"}
        else (90 if path == "activity-timeline" else 730)
    )
    end = min(bounds.to, now)
    if bounds.to - bounds.from_ > timedelta(days=days) or bounds.from_ >= end:
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid owner query range")
    return OwnerQuery(bounds.from_, end, bounds.to, validate_timezone(tz), now)


def result(query: OwnerQuery, method: str, **kwargs: Any) -> OwnerResult:
    return OwnerResult(query=query.echo(), as_of=query.now, method=method, **kwargs)


def supported(request: Request, feature: str) -> bool:
    return owner_supported(request.app.state.compatibility.owner_columns, feature)


def cached_owner_response(request: Request) -> Response | None:
    body = request.app.state.cache.get((request.url.path, request.url.query))
    return conditional_response(request, body, ttl=30) if body is not None else None


def normalize_numbers(item: Any) -> Any:
    if isinstance(item, Decimal):
        return float(item)
    if isinstance(item, dict):
        return {key: normalize_numbers(value) for key, value in item.items()}
    if isinstance(item, list):
        return [normalize_numbers(value) for value in item]
    return item


async def respond(request: Request, body: OwnerResult) -> Response:
    async def render() -> bytes:
        return to_json(normalize_numbers(body.model_dump(mode="python")))

    return await cached_response(
        request,
        cache=request.app.state.cache,
        key=(request.url.path, request.url.query),
        render=render,
        ttl=30,
    )


def binding(request: Request, vehicle: int, method: str, query: OwnerQuery | None) -> str:
    # Original requested upper bound is stable even while the effective bound advances.
    identity = {
        key: value for key, value in request.query_params.items() if key not in {"cursor", "limit"}
    }
    return hashlib.sha256(
        json.dumps([request.url.path, vehicle, method, identity], sort_keys=True).encode()
    ).hexdigest()


async def history(
    request: Request,
    vehicle: VehicleIdentity,
    query: OwnerQuery,
    pool: asyncpg.Pool,
    max_points: str,
    *,
    environment: bool,
) -> Response:
    size = bounded_integer(max_points, default_name="max_points", minimum=2, maximum=1000)
    feature = "environment_history" if environment else "battery_history"
    method = "environment-history-v1" if environment else "position-history-v1"
    if not supported(request, feature):
        return await respond(request, result(query, method, status="unsupported"))
    cached = cached_owner_response(request)
    if cached is not None:
        return cached
    names = (
        ("inside_temp_c", "outside_temp_c")
        if environment
        else ("soc_pct", "usable_soc_pct", "rated_range_km", "ideal_range_km")
    )
    projection = ",".join(f"{POSITION_FIELDS[name][0]} AS {name}" for name in names)
    source = (
        "(SELECT DISTINCT ON (date) id,date,"
        + projection
        + " FROM positions WHERE car_id=$1 AND date >= $2 AT TIME ZONE 'UTC' "
        "AND date < $3 AT TIME ZONE 'UTC' ORDER BY date,id DESC) observations"
    )
    args = (vehicle.id, query.start, query.end)
    async with pool.acquire() as db:
        counts = await fetch_rows(
            db,
            "SELECT count(*) AS raw,count(DISTINCT date) AS unique,"
            "max(date) AT TIME ZONE 'UTC' AS last,"
            + ",".join(f"count({POSITION_FIELDS[name][0]}) AS {name}_valid" for name in names)
            + " FROM positions "
            "WHERE car_id=$1 AND date >= $2 AT TIME ZONE 'UTC' "
            "AND date < $3 AT TIME ZONE 'UTC'",
            *args,
        )
        sampled = await fetch_series(
            db,
            table=source,
            time_column="date",
            columns={**{name: name for name in names}, "source_id": "id"},
            where="true",
            args=args,
            fields=",".join(names),
            required_fields=("source_id",),
            points=size,
        )
        gap_rows = await fetch_rows(
            db,
            "WITH times AS (SELECT DISTINCT date AT TIME ZONE 'UTC' AS t FROM positions "
            "WHERE car_id=$1 AND date >= $2 AT TIME ZONE 'UTC' AND date < $3 AT TIME ZONE 'UTC'),"
            " edges AS (SELECT t,lag(t,1,$2::timestamptz) OVER (ORDER BY t) AS prev FROM "
            "(SELECT t FROM times UNION SELECT $3::timestamptz) all_times), gaps AS "
            "(SELECT prev AS started_at,t AS ended_at FROM edges "
            "WHERE t-prev>interval '300 seconds')"
            " SELECT *,count(*) OVER () AS total FROM gaps ORDER BY started_at LIMIT 1000",
            *args,
        )
    items = [
        {
            "at": row["t"],
            **{name: row[name] for name in names},
            "source": "position",
            "source_id": int(row["source_id"]),
        }
        for row in sampled.series
    ]
    count = counts[0]
    return await respond(
        request,
        result(
            query,
            method,
            status="available" if items else "no_records",
            items=items,
            source_last_at=count["last"],
            raw_points=count["raw"],
            deduplicated_points=count["unique"],
            output_points=len(items),
            gaps={
                "items": [
                    {"started_at": r["started_at"], "ended_at": r["ended_at"]} for r in gap_rows
                ],
                "truncated": bool(gap_rows and gap_rows[0]["total"] > 1000),
            },
            coverage={
                name: {
                    "candidate": count["raw"],
                    "valid": count[name + "_valid"],
                    "status": "available"
                    if count[name + "_valid"]
                    else "insufficient_data"
                    if count["raw"]
                    else "no_records",
                }
                for name in names
            },
        ),
    )


@router.get("/battery-history", response_model=OwnerResult[BatteryHistoryPoint])
async def battery_history(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    query: Annotated[OwnerQuery, Depends(owner_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    max_points: str = "480",
) -> Response:
    return await history(request, vehicle, query, pool, max_points, environment=False)


@router.get("/environment-history", response_model=OwnerResult[EnvironmentHistoryPoint])
async def environment_history(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    query: Annotated[OwnerQuery, Depends(owner_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    max_points: str = "480",
) -> Response:
    return await history(request, vehicle, query, pool, max_points, environment=True)


@router.get("/activity-timeline", response_model=OwnerResult[ActivityEvent])
async def activity_timeline(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    query: Annotated[OwnerQuery, Depends(owner_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    limit: str = "20",
    cursor: str | None = None,
) -> Response:
    size = bounded_integer(limit, default_name="limit", minimum=1, maximum=100)
    identity = binding(request, vehicle.id, "activity-timeline-v1", query)
    key = decode_bound_cursor(cursor, identity)
    time, kind, identifier = None, None, None
    if key is not None:
        try:
            time = datetime.fromisoformat(key[0])
            if time.tzinfo is None:
                raise ValueError
            time = time.astimezone(UTC)
            kind, identifier = key[1:]
            if (
                len(key) != 3
                or kind not in {"drive", "charging_session", "state_segment"}
                or (type(identifier) is not int or not 0 < identifier <= 2147483647)
            ):
                raise ValueError
        except (TypeError, ValueError, IndexError, OverflowError):
            raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid timeline cursor") from None
    if not supported(request, "activity_timeline"):
        return await respond(request, result(query, "activity-timeline-v1", status="unsupported"))
    cached = cached_owner_response(request)
    if cached is not None:
        return cached
    parts = [
        f"SELECT id,car_id,start_date,end_date,'{kind}'::text AS kind,"
        + ("state::text" if table == "states" else "NULL::text")
        + f" AS state FROM {table}"
        for table, kind in (
            ("drives", "drive"),
            ("charging_processes", "charging_session"),
            ("states", "state_segment"),
        )
    ]
    async with pool.acquire() as db:
        rows = await fetch_rows(
            db,
            "WITH events AS ("
            + " UNION ALL ".join(parts)
            + "), matched AS (SELECT *,start_date AT TIME ZONE 'UTC' AS started_at, "
            "end_date AT TIME ZONE 'UTC' AS ended_at FROM events WHERE car_id=$1 "
            "AND start_date < $3 AT TIME ZONE 'UTC' "
            "AND (end_date IS NULL OR end_date > $2 AT TIME ZONE 'UTC')) "
            "SELECT *,(SELECT max(started_at) FROM matched) AS last FROM matched "
            "WHERE ($4::timestamptz IS NULL OR (started_at,kind,id)<($4,$5::text,$6::int)) "
            "ORDER BY started_at DESC,kind DESC,id DESC LIMIT $7",
            vehicle.id,
            query.start,
            query.end,
            time,
            kind,
            identifier,
            size + 1,
        )
    items = [
        {
            "event_id": f"{r['kind']}:{r['id']}",
            "kind": r["kind"],
            "record_id": r["id"],
            "state": r["state"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "intersection_start": max(r["started_at"], query.start),
            "intersection_end": min(r["ended_at"] or query.end, query.end),
            "status": "completed" if r["ended_at"] else "ongoing",
        }
        for r in rows[:size]
    ]
    next_cursor = None
    if len(rows) > size:
        r = rows[size - 1]
        next_cursor = encode_bound_cursor(
            identity, [r["started_at"].isoformat(), r["kind"], r["id"]]
        )
    return await respond(
        request,
        result(
            query,
            "activity-timeline-v1",
            status="available" if items else "no_records",
            items=items,
            next_cursor=next_cursor,
            source_last_at=rows[0]["last"] if rows else None,
        ),
    )


@router.get("/vehicle-observations/latest", response_model=OwnerResult[LatestObservation])
async def latest_observation(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    now: Annotated[datetime, Depends(get_now)],
) -> Response:
    cached = cached_owner_response(request)
    if cached is not None:
        return cached
    columns = request.app.state.compatibility.owner_columns
    names = [name for name in POSITION_FIELDS if name.endswith(("_c", "_bar"))]
    projection = ",".join(
        f"{column if ('positions', column) in columns else 'NULL::numeric'} AS {name}"
        for name in names
        for column, _ in [POSITION_FIELDS[name]]
    )
    async with pool.acquire() as db:
        rows = await fetch_rows(
            db,
            f"SELECT id,date AT TIME ZONE 'UTC' AS sampled_at,{projection} "
            "FROM positions WHERE car_id=$1 AND date <= $2 AT TIME ZONE 'UTC' "
            "ORDER BY date DESC,id DESC LIMIT 1",
            vehicle.id,
            now,
        )
    metrics = {}
    for name in names:
        available = ("positions", POSITION_FIELDS[name][0]) in columns
        value = rows[0][name] if rows else None
        metrics[name] = {
            "value": value,
            "unit": "bar" if name.endswith("_bar") else "°C",
            "sampled_at": rows[0]["sampled_at"] if rows else None,
            "source": "position",
            "coverage": {"candidate": int(bool(rows)), "valid": int(value is not None)},
            "status": "unsupported"
            if not available
            else "available"
            if value is not None
            else "insufficient_data",
        }
    body: OwnerResult = OwnerResult(
        query={"vehicle_id": vehicle.id},
        as_of=now,
        method="latest-position-observation-v1",
        status="available" if rows else "no_records",
        source_last_at=rows[0]["sampled_at"] if rows else None,
        items=[
            {
                "source": "position",
                "source_id": rows[0]["id"],
                "sampled_at": rows[0]["sampled_at"],
                "metrics": metrics,
            }
        ]
        if rows
        else [],
    )
    return await respond(request, body)
