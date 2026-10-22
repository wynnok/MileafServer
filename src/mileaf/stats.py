"""Calendar statistics skeleton; D1/D2 fill the reserved metric fields."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.responses import Response

from mileaf.cache import conditional_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.probes import CAPABILITIES
from mileaf.queries.time import Bucket, bucket_sql, validate_timezone
from mileaf.units import distance_km, distance_km_sql
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

CAPABILITIES["statistics"] = {
    "drives": {
        "car_id": ("int2",),
        "start_date": ("timestamp",),
        "end_date": ("timestamp",),
        "distance": ("float8",),
    },
    "charging_processes": {"car_id": ("int2",), "end_date": ("timestamp",)},
}


class StatisticsRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def validate(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise APIError(
                    400, ErrorCode.INVALID_REQUEST, "Invalid statistics parameters"
                ) from None

        return validate


router = APIRouter(prefix="/v1", tags=["statistics"], route_class=StatisticsRoute)


class DrivingMetrics(BaseModel):
    drives: int
    distance_km: float
    energy_kwh: float | None = None
    consumption_wh_km_net: float | None = None
    consumption_wh_km_gross: float | None = None
    speed_max_kmh: float | None = None


class ChargingMetrics(BaseModel):
    sessions: int
    energy_added_kwh: float | None = None
    cost: float | None = None
    cost_per_kwh: float | None = None
    ac_energy_kwh: float | None = None
    dc_energy_kwh: float | None = None
    dc_energy_share: float | None = None


class DrivingBucket(DrivingMetrics):
    bucket_start: datetime


class ChargingBucket(ChargingMetrics):
    bucket_start: datetime


class DrivingStats(BaseModel):
    buckets: list[DrivingBucket]
    totals: DrivingMetrics


class ChargingStats(BaseModel):
    buckets: list[ChargingBucket]
    totals: ChargingMetrics


@dataclass(frozen=True)
class StatsParameters:
    kind: Literal["driving", "charging"]
    bucket: Bucket
    from_: datetime
    to: datetime
    tz: str


def stats_parameters(
    kind: Annotated[str, Query(json_schema_extra={"enum": ["driving", "charging"]})],
    bucket: Annotated[str, Query(json_schema_extra={"enum": ["day", "week", "month", "year"]})],
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
    tz: str = "UTC",
) -> StatsParameters:
    if kind not in {"driving", "charging"} or bucket not in {"day", "week", "month", "year"}:
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid kind or bucket")
    try:
        start, end = datetime.fromisoformat(from_), datetime.fromisoformat(to)
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if not timedelta(0) < end - start <= timedelta(days=730):
            raise ValueError
    except (ValueError, OverflowError):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid statistics time range") from None
    return StatsParameters(
        cast(Literal["driving", "charging"], kind),
        cast(Bucket, bucket),
        start,
        end,
        validate_timezone(tz),
    )


def ratio_sql(*, numerator: str, denominator: str) -> str:
    """Trusted additive SQL terms: combine first, divide once for each grouping set.

    D2 can use this for cost_per_kwh and dc_energy_share. Missing/zero denominator
    produces NULL; integer terms are promoted to numeric before division.
    """
    return f"(sum({numerator}))::numeric / nullif(sum({denominator}), 0)"


@router.get("/vehicles/{vehicle_id}/stats", response_model=DrivingStats | ChargingStats)
async def statistics(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    params: Annotated[StatsParameters, Depends(stats_parameters)],
) -> Response:
    driving = params.kind == "driving"
    table = "drives" if driving else "charging_processes"
    time_column = "start_date" if driving else "end_date"
    bucket = bucket_sql(params.bucket, timestamp=f"{time_column} AT TIME ZONE 'UTC'", tz="$4")
    # GROUPING SETS calculates bucket values and totals from the same filtered rows;
    # future ratios must use ratio_sql so totals never average bucket ratios.
    distance = f", coalesce(sum({distance_km_sql('distance')}),0) AS distance_km" if driving else ""
    count_name = "drives" if driving else "sessions"
    query = (
        "/* TeslaMate v4.3.0 drive-stats / charging-stats semantics, independently rewritten: "
        "count finished records; sum stored driving distance (km). Drives use start time, "
        "charging sessions use end time; UTC [from,to), local calendar buckets. */ "
        f"WITH source AS (SELECT {bucket} AS bucket_start"
        + (", distance" if driving else "")
        + f" FROM {table} WHERE car_id=$1 AND end_date IS NOT NULL "
        f"AND {time_column} AT TIME ZONE 'UTC' >= $2::timestamptz "
        f"AND {time_column} AT TIME ZONE 'UTC' < $3::timestamptz), "
        f"aggregated AS (SELECT bucket_start, grouping(bucket_start)=1 AS is_total, "
        f"count(*) AS {count_name}{distance} "
        "FROM source GROUP BY GROUPING SETS ((bucket_start), ())), "
        "calendar AS (SELECT local_start AT TIME ZONE $4 AS bucket_start FROM generate_series("
        f"date_trunc('{params.bucket}', $2::timestamptz AT TIME ZONE $4), "
        f"date_trunc('{params.bucket}', $3::timestamptz AT TIME ZONE $4), "
        f"interval '1 {params.bucket}') AS local_start "
        "WHERE local_start AT TIME ZONE $4 < $3::timestamptz) "
        f"SELECT c.bucket_start, false AS is_total, coalesce(a.{count_name},0) AS {count_name}"
        + (", coalesce(a.distance_km,0) AS distance_km" if driving else "")
        + " FROM calendar c LEFT JOIN aggregated a "
        "ON a.bucket_start=c.bucket_start AND NOT a.is_total UNION ALL "
        f"SELECT bucket_start, is_total, {count_name}"
        + (", distance_km" if driving else "")
        + " FROM aggregated WHERE is_total ORDER BY is_total, bucket_start"
    )
    async with pool.acquire() as connection:
        rows = await fetch_rows(connection, query, vehicle.id, params.from_, params.to, params.tz)
    if driving:
        result: DrivingStats | ChargingStats = DrivingStats(
            buckets=[
                DrivingBucket(
                    bucket_start=row["bucket_start"],
                    drives=row["drives"],
                    distance_km=distance_km(row["distance_km"]),
                )
                for row in rows
                if not row["is_total"]
            ],
            totals=DrivingMetrics(
                drives=rows[-1]["drives"], distance_km=distance_km(rows[-1]["distance_km"])
            ),
        )
    else:
        result = ChargingStats(
            buckets=[
                ChargingBucket(
                    bucket_start=row["bucket_start"],
                    sessions=row["sessions"],
                )
                for row in rows
                if not row["is_total"]
            ],
            totals=ChargingMetrics(sessions=rows[-1]["sessions"]),
        )
    return conditional_response(request, result.model_dump_json().encode(), ttl=30)
