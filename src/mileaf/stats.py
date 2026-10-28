"""Calendar statistics aggregated in PostgreSQL before presentation rounding."""

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
from mileaf.queries.charging_metrics import ChargingMetrics, charging_metrics_sql
from mileaf.queries.charging_query import (
    ChargingQuery,
    ChargingQueryEcho,
    charging_parameters,
    match_source,
)
from mileaf.queries.energy import energy_kwh_sql
from mileaf.queries.time import Bucket, bucket_sql, validate_timezone
from mileaf.units import consumption_wh_km, distance_km, distance_km_sql, speed_kmh
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

CAPABILITIES["statistics"] = {
    "drives": {
        "car_id": ("int2",),
        "start_date": ("timestamp",),
        "end_date": ("timestamp",),
        "distance": ("float8",),
        "speed_max": ("int2",),
    },
    "charging_processes": {
        "car_id": ("int2",),
        "end_date": ("timestamp",),
        "charge_energy_added": ("numeric",),
        "cost": ("numeric",),
    },
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
    energy_kwh: float | None
    consumption_wh_km_net: float | None
    consumption_wh_km_gross: float | None
    speed_max_kmh: float | None


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
    query: ChargingQueryEcho
    as_of: datetime


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
    """Combine trusted additive terms before dividing; missing/zero divisor => NULL."""
    return f"(sum({numerator}))::numeric / nullif(sum({denominator}), 0)"


@router.get("/vehicles/{vehicle_id}/stats", response_model=DrivingStats | ChargingStats)
async def statistics(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    params: Annotated[StatsParameters, Depends(stats_parameters)],
    charging: Annotated[ChargingQuery, Depends(charging_parameters)],
) -> Response:
    driving = params.kind == "driving"
    args: list[Any] = [vehicle.id, params.from_, params.to, params.tz]
    drive_bucket = bucket_sql(params.bucket, timestamp="d.start_date AT TIME ZONE 'UTC'", tz="$4")
    charge_bucket = bucket_sql(params.bucket, timestamp="cp.end_date AT TIME ZONE 'UTC'", tz="$4")
    if driving:
        # Union contributions before grouping: joining drives to sessions would multiply
        # both energies. A charging-only bucket must still contribute to gross totals.
        source = (
            "/* TeslaMate v4.3.0 drives / overview, independently rewritten: finished "
            "drives by start_date; preferred range delta * efficiency -> net kWh "
            "(non-positive delta/missing efficiency => NULL); sum kWh / sum km * 1000 "
            "-> net Wh/km. Overview gross uses finished charge_energy_added by end_date "
            "/ driving km * 1000; no charging energy or zero km => NULL. "
            "Stored distance/speed are km/km/h regardless of display preference. */ "
            f"SELECT {drive_bucket} AS bucket_start, 1 AS drives, "
            f"{distance_km_sql('d.distance')} AS distance_km, {energy_kwh_sql()} AS energy_kwh, "
            "d.speed_max AS speed_max_kmh, NULL::numeric AS gross_energy_kwh "
            "FROM drives d JOIN cars c ON c.id=d.car_id CROSS JOIN settings s "
            "WHERE d.car_id=$1 AND d.end_date IS NOT NULL "
            "AND d.start_date AT TIME ZONE 'UTC' >= $2::timestamptz "
            "AND d.start_date AT TIME ZONE 'UTC' < $3::timestamptz UNION ALL "
            f"SELECT {charge_bucket}, 0, NULL, NULL, NULL, cp.charge_energy_added "
            "FROM charging_processes cp WHERE cp.car_id=$1 AND cp.end_date IS NOT NULL "
            "AND cp.end_date AT TIME ZONE 'UTC' >= $2::timestamptz "
            "AND cp.end_date AT TIME ZONE 'UTC' < $3::timestamptz"
        )
        metrics = {
            "drives": "coalesce(sum(drives),0)",
            "distance_km": "coalesce(sum(distance_km),0)",
            "energy_kwh": "CASE WHEN coalesce(sum(drives),0)=0 THEN 0 ELSE sum(energy_kwh) END",
            "consumption_wh_km_net": ratio_sql(numerator="energy_kwh", denominator="distance_km")
            + " * 1000",
            "consumption_wh_km_gross": ratio_sql(
                numerator="nullif(gross_energy_kwh,0)", denominator="distance_km"
            )
            + " * 1000",
            "speed_max_kmh": "max(speed_max_kmh)",
        }
        zero_metrics = {"drives", "distance_km", "energy_kwh"}
    else:
        matched, args = match_source(charging, vehicle.id, completed=True)
        args.append(params.tz)
        charge_time = "started_at" if charging.echo.time_basis == "started_at" else "ended_at"
        charge_bucket = bucket_sql(params.bucket, timestamp=charge_time, tz="$8")
        source = f"SELECT *, {charge_bucket} AS bucket_start FROM ({matched}) matched"
        metrics = charging_metrics_sql()
        zero_metrics = set(metrics) - {
            "cost_per_kwh",
            "dc_energy_share",
            "unit_cost_added",
            "unit_cost_grid",
        }
    # GROUPING SETS applies the same formulas to buckets and the full window.
    aggregates = ", ".join(f"{expression} AS {name}" for name, expression in metrics.items())
    projected = ", ".join(
        f"CASE WHEN a.bucket_start IS NULL THEN 0 ELSE a.{name} END AS {name}"
        if name in zero_metrics
        else f"coalesce(a.{name}, (SELECT {expression} FROM source WHERE false)) AS {name}"
        if name.startswith("unit_cost_")
        else f"a.{name}"
        for name, expression in metrics.items()
    )
    tz_parameter = "$4" if driving else "$8"
    query = (
        f"WITH source AS ({source}), "
        f"aggregated AS (SELECT bucket_start, grouping(bucket_start)=1 AS is_total, {aggregates} "
        "FROM source GROUP BY GROUPING SETS ((bucket_start), ())), "
        f"calendar AS (SELECT local_start AT TIME ZONE {tz_parameter} AS bucket_start "
        "FROM generate_series("
        f"date_trunc('{params.bucket}', $2::timestamptz AT TIME ZONE {tz_parameter}), "
        f"date_trunc('{params.bucket}', $3::timestamptz AT TIME ZONE {tz_parameter}), "
        f"interval '1 {params.bucket}') AS local_start "
        f"WHERE local_start AT TIME ZONE {tz_parameter} < $3::timestamptz) "
        f"SELECT c.bucket_start, false AS is_total, {projected} "
        "FROM calendar c LEFT JOIN aggregated a "
        "ON a.bucket_start=c.bucket_start AND NOT a.is_total UNION ALL "
        f"SELECT bucket_start, is_total, {', '.join(metrics)} "
        "FROM aggregated WHERE is_total ORDER BY is_total, bucket_start"
    )
    async with pool.acquire() as connection:
        rows = await fetch_rows(connection, query, *args)
    if driving:
        driving_rows = []
        for row in rows:
            data = dict(row)
            data["distance_km"] = distance_km(data["distance_km"])
            data["speed_max_kmh"] = speed_kmh(data["speed_max_kmh"])
            for name in ("consumption_wh_km_net", "consumption_wh_km_gross"):
                data[name] = consumption_wh_km(data[name])
            driving_rows.append(data)
        result: DrivingStats | ChargingStats = DrivingStats(
            buckets=[DrivingBucket(**row) for row in driving_rows if not row["is_total"]],
            totals=DrivingMetrics(**driving_rows[-1]),
        )
    else:
        result = ChargingStats(
            buckets=[ChargingBucket(**dict(row)) for row in rows if not row["is_total"]],
            totals=ChargingMetrics(**dict(rows[-1])),
            query=charging.echo,
            as_of=charging.as_of,
        )
    return conditional_response(
        request,
        result.model_dump_json(by_alias=True).encode(),
        ttl=30,
        validator_body=(
            result.model_dump_json(by_alias=True, exclude={"as_of"}).encode()
            if not driving
            else None
        ),
    )
