"""Completed-session rated range observations; never a battery health estimate."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from mileaf.cache import conditional_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_now, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.queries.parameters import ResourceRoute
from mileaf.queries.statistics_fields import (
    STATISTICS_REQUIREMENTS,
    statistics_feature_supported,
)
from mileaf.queries.time import validate_timezone
from mileaf.units import Number, distance_km
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

STATISTICS_REQUIREMENTS["range_observations"] = {
    "charges": {
        "charging_process_id": ("int4",),
        "date": ("timestamp",),
        "battery_level": ("int2",),
        "rated_battery_range_km": ("numeric",),
    },
    "charging_processes": {
        "id": ("int4",),
        "car_id": ("int2",),
        "start_date": ("timestamp",),
        "end_date": ("timestamp",),
    },
}
router = APIRouter(prefix="/v1", tags=["statistics"], route_class=ResourceRoute)

ExclusionReason = Literal["missing_soc", "soc_out_of_range", "invalid_rated_range"]
Quality = Literal["available", "no_records", "insufficient_data", "unsupported"]


class RangeThresholds(BaseModel):
    soc_min: int = Field(20, description="Inclusive lower battery_level bound, percent")
    soc_max: int = Field(90, description="Inclusive upper battery_level bound, percent")
    minimum_valid_days: int = Field(3, description="Valid days per month before sparse")
    validation: Literal["provisional"] = "provisional"


class RangeCoverage(BaseModel):
    candidate_points: int = Field(0, description="Charge samples inside completed session spans")
    valid_points: int = Field(0, description="Samples with SOC 20..90 and finite rated range > 0")
    excluded_points: int = Field(0, description="candidate_points - valid_points")
    candidate_sessions: int = Field(0, description="Completed sessions ending inside the month")
    valid_sessions: int = Field(0, description="Sessions with at least one valid sample")
    valid_days: int = Field(0, description="Local end days with at least one valid session")
    excluded_by_reason: dict[ExclusionReason, int] = Field(
        default_factory=dict, description="Point exclusions; sums to excluded_points"
    )


class RangeSummary(BaseModel):
    status: Quality = Field(description="unsupported wins over data volume; see #34 §2")
    sample_status: Literal["adequate", "sparse", "insufficient_data"] = Field(
        "insufficient_data", description="sparse below three valid days"
    )
    median_range_km: float | None = Field(
        None, description="km; percentile_cont over local day values, null without valid days"
    )
    p10_range_km: float | None = Field(None, description="km; percentile_cont(0.1) over day values")
    p90_range_km: float | None = Field(None, description="km; percentile_cont(0.9) over day values")
    coverage: RangeCoverage = Field(default_factory=RangeCoverage)
    method: Literal["session-day-rated-range-v1"] = Field(
        "session-day-rated-range-v1",
        description="Session point median, then local end day median, then month day quantiles",
    )
    version: int = 1
    thresholds: RangeThresholds = Field(default_factory=RangeThresholds)


class RangeBucket(RangeSummary):
    bucket_start: datetime = Field(description="UTC instant of the local month start")
    partial: bool = Field(description="True when the effective cutoff ends this month early")


class RangeQuery(BaseModel):
    vehicle_id: int
    from_: datetime = Field(serialization_alias="from")
    to: datetime
    tz: str
    bucket: Literal["month"] = "month"
    filters: dict[str, int]
    time_basis: Literal["ended_at"] = "ended_at"


class RangeObservations(BaseModel):
    query: RangeQuery
    as_of: datetime
    range_kind: Literal["rated"] = "rated"
    soc_source: Literal["battery_level"] = "battery_level"
    first_observed_at: datetime | None = Field(
        None, description="Earliest valid sample UTC instant inside the query window"
    )
    first_observed_scope: Literal["query"] = Field(
        "query", description="Never a new-car baseline; scoped to the requested window"
    )
    ongoing_sessions: int = Field(
        0, description="Sessions started inside the window that have not ended"
    )
    method: Literal["session-day-rated-range-v1"] = "session-day-rated-range-v1"
    version: int = 1
    thresholds: RangeThresholds = Field(default_factory=RangeThresholds)
    buckets: list[RangeBucket]
    totals: RangeSummary


@dataclass(frozen=True)
class MonthWindow:
    key: date
    start: datetime
    partial: bool


@dataclass(frozen=True)
class RangeParameters:
    start: datetime
    end: datetime
    tz: str
    months: int
    as_of: datetime
    buckets: tuple[MonthWindow, ...]


def month_windows(
    first: date, count: int, end: datetime, zone: ZoneInfo
) -> tuple[MonthWindow, ...]:
    """Local calendar months rendered as UTC instants; the last month may be partial."""
    index = first.year * 12 + first.month - 1
    starts = [
        datetime((index + step) // 12, (index + step) % 12 + 1, 1, tzinfo=zone).astimezone(UTC)
        for step in range(count + 1)
    ]
    keys = [date((index + step) // 12, (index + step) % 12 + 1, 1) for step in range(count)]
    return tuple(
        MonthWindow(key, start, limit > end)
        for key, start, limit in zip(keys, starts[:-1], starts[1:], strict=True)
    )


def numeric(observed: dict[str, object] | None, column: str) -> Number | None:
    """Keep PostgreSQL's unrounded aggregate type; rounding happens at the DTO edge."""
    value = (observed or {}).get(column)
    return value if isinstance(value, int | float | Decimal) else None


def range_parameters(
    now: Annotated[datetime, Depends(get_now)],
    months: Annotated[Literal["6", "12"], Query()],
    to: str | None = None,
    tz: str = "UTC",
) -> RangeParameters:
    validate_timezone(tz)
    month_count = int(months)
    try:
        end = datetime.fromisoformat(to) if to is not None else now
        if end.tzinfo is None:
            raise ValueError
        end = min(end.astimezone(UTC), now)
        zone = ZoneInfo(tz)
        last = (end - timedelta(microseconds=1)).astimezone(zone)
        first = date(last.year, last.month, 1)
        offset = first.year * 12 + first.month - month_count
        start = datetime(offset // 12, offset % 12 + 1, 1, tzinfo=zone)
        if start.astimezone(UTC) >= end:
            raise ValueError
    except (ValueError, OverflowError):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid observation window") from None
    return RangeParameters(
        start.astimezone(UTC),
        end,
        tz,
        month_count,
        now,
        month_windows(date(offset // 12, offset % 12 + 1, 1), month_count, end, zone),
    )


def range_observations_sql() -> str:
    """Session, local day and month medians in one bounded statement (budget: 2)."""
    return (
        "WITH sessions AS (SELECT cp.id AS session_id, cp.start_date AS lower_bound, "
        "cp.end_date AS upper_bound, "
        "date_trunc('month',(cp.end_date AT TIME ZONE 'UTC') AT TIME ZONE $2)::date AS month_key, "
        "date_trunc('day',(cp.end_date AT TIME ZONE 'UTC') AT TIME ZONE $2)::date AS day_key "
        "FROM charging_processes cp WHERE cp.car_id=$1 AND cp.end_date IS NOT NULL "
        "AND (cp.end_date AT TIME ZONE 'UTC')>=$3 "
        "AND (cp.end_date AT TIME ZONE 'UTC')<$4), "
        "raw_points AS (SELECT s.session_id, s.month_key, s.day_key, "
        "(c.date AT TIME ZONE 'UTC') AS sample_at, c.battery_level AS soc, "
        "c.rated_battery_range_km AS rated_km, "
        "CASE WHEN c.battery_level IS NULL THEN 'missing_soc' "
        "WHEN c.battery_level<20 OR c.battery_level>90 THEN 'soc_out_of_range' "
        "WHEN c.rated_battery_range_km IS NULL OR NOT (c.rated_battery_range_km>0 "
        "AND c.rated_battery_range_km<'Infinity'::float8) THEN 'invalid_rated_range' END "
        "AS exclusion "
        "FROM sessions s JOIN charges c ON c.charging_process_id=s.session_id "
        "AND c.date>=s.lower_bound AND c.date<=s.upper_bound), "
        "points AS (SELECT *, CASE WHEN exclusion IS NULL THEN rated_km*100/soc END AS value_km "
        "FROM raw_points), "
        "session_values AS (SELECT month_key, day_key, session_id, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY value_km) AS session_km FROM points "
        "WHERE exclusion IS NULL GROUP BY month_key, day_key, session_id), "
        "day_values AS (SELECT month_key, day_key, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY session_km) AS day_km "
        "FROM session_values GROUP BY month_key, day_key), "
        "months AS (SELECT s.month_key, s.candidate_sessions, "
        "coalesce(p.candidate_points,0) AS candidate_points, "
        "coalesce(p.valid_points,0) AS valid_points, "
        "coalesce(v.valid_sessions,0) AS valid_sessions, "
        "coalesce(d.valid_days,0) AS valid_days, "
        "coalesce(r.excluded_by_reason,'{}'::jsonb) AS excluded_by_reason, "
        "q.median_km, q.p10_km, q.p90_km FROM "
        "(SELECT month_key,count(*) AS candidate_sessions FROM sessions GROUP BY month_key) s "
        "LEFT JOIN (SELECT month_key,count(*) AS candidate_points,"
        "count(value_km) AS valid_points FROM points GROUP BY month_key) p USING(month_key) "
        "LEFT JOIN (SELECT month_key,count(*) AS valid_sessions FROM session_values "
        "GROUP BY month_key) v USING(month_key) "
        "LEFT JOIN (SELECT month_key,count(*) AS valid_days FROM day_values "
        "GROUP BY month_key) d USING(month_key) "
        "LEFT JOIN (SELECT month_key,percentile_cont(0.5) WITHIN GROUP (ORDER BY day_km) "
        "AS median_km,percentile_cont(0.1) WITHIN GROUP (ORDER BY day_km) AS p10_km,"
        "percentile_cont(0.9) WITHIN GROUP (ORDER BY day_km) AS p90_km FROM day_values "
        "GROUP BY month_key) q USING(month_key) "
        "LEFT JOIN (SELECT month_key,jsonb_object_agg(exclusion,counted) AS excluded_by_reason "
        "FROM (SELECT month_key,exclusion,count(*) AS counted FROM points "
        "WHERE exclusion IS NOT NULL GROUP BY month_key,exclusion) g GROUP BY month_key) r "
        "USING(month_key)) "
        "SELECT (SELECT coalesce(jsonb_agg(to_jsonb(m) ORDER BY m.month_key),'[]'::jsonb) "
        "FROM months m) AS months, "
        "(SELECT count(*) FROM sessions) AS candidate_sessions, "
        "(SELECT count(*) FROM points) AS candidate_points, "
        "(SELECT count(value_km) FROM points) AS valid_points, "
        "(SELECT count(*) FROM session_values) AS valid_sessions, "
        "(SELECT count(*) FROM day_values) AS valid_days, "
        "(SELECT coalesce(jsonb_object_agg(exclusion,counted),'{}'::jsonb) "
        "FROM (SELECT exclusion,count(*) AS counted FROM points WHERE exclusion IS NOT NULL "
        "GROUP BY exclusion) g) AS excluded_by_reason, "
        "(SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY day_km) FROM day_values) "
        "AS median_km, "
        "(SELECT percentile_cont(0.1) WITHIN GROUP (ORDER BY day_km) FROM day_values) AS p10_km, "
        "(SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY day_km) FROM day_values) AS p90_km, "
        "(SELECT min(sample_at) FROM points WHERE exclusion IS NULL "
        "AND sample_at>=$3 AND sample_at<$4) AS first_observed_at, "
        "(SELECT count(*) FROM charging_processes WHERE car_id=$1 AND end_date IS NULL "
        "AND (start_date AT TIME ZONE 'UTC')>=$3 AND (start_date AT TIME ZONE 'UTC')<$4) "
        "AS ongoing_sessions"
    )


def summary(observed: dict[str, object] | None, *, supported: bool) -> RangeSummary:
    """Status precedence: unsupported, then no candidate, then no valid day.

    `observed` maps aggregate column names to values; None is an empty month.
    """
    coverage = RangeCoverage.model_validate(
        {
            "candidate_points": 0,
            "valid_points": 0,
            "candidate_sessions": 0,
            "valid_sessions": 0,
            "valid_days": 0,
            "excluded_by_reason": {},
            **(observed or {}),
        }
    )
    coverage.excluded_points = coverage.candidate_points - coverage.valid_points
    if not supported:
        status: Quality = "unsupported"
    elif coverage.candidate_sessions == 0 and coverage.candidate_points == 0:
        status = "no_records"
    elif coverage.valid_days == 0:
        status = "insufficient_data"
    else:
        status = "available"
    return RangeSummary(
        status=status,
        sample_status="adequate"
        if coverage.valid_days >= RangeThresholds().minimum_valid_days
        else "sparse"
        if status == "available"
        else "insufficient_data",
        median_range_km=distance_km(numeric(observed, "median_km")),
        p10_range_km=distance_km(numeric(observed, "p10_km")),
        p90_range_km=distance_km(numeric(observed, "p90_km")),
        coverage=coverage,
    )


@router.get("/vehicles/{vehicle_id}/range-observations", response_model=RangeObservations)
async def range_observations(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    params: Annotated[RangeParameters, Depends(range_parameters)],
) -> Response:
    supported = statistics_feature_supported(
        request.app.state.compatibility.statistics_columns, "range_observations"
    )
    months: list[dict[str, object]] = []
    aggregate: dict[str, object] = {}
    first_observed_at = None
    ongoing = 0
    if supported:
        async with pool.acquire() as db:
            row = (
                await fetch_rows(
                    db,
                    range_observations_sql(),
                    vehicle.id,
                    params.tz,
                    params.start,
                    params.end,
                )
            )[0]
        months = json.loads(row["months"])
        aggregate = dict(row)
        aggregate["excluded_by_reason"] = json.loads(row["excluded_by_reason"])
        first_observed_at = row["first_observed_at"]
        ongoing = row["ongoing_sessions"]
    totals = summary(aggregate or None, supported=supported)
    observed = {str(month["month_key"]): month for month in months}
    buckets = [
        RangeBucket(
            **summary(observed.get(window.key.isoformat()), supported=supported).model_dump(),
            bucket_start=window.start,
            partial=window.partial,
        )
        for window in params.buckets
    ]
    result = RangeObservations(
        query=RangeQuery(
            vehicle_id=vehicle.id,
            from_=params.start,
            to=params.end,
            tz=params.tz,
            filters={"months": params.months},
        ),
        as_of=params.as_of,
        first_observed_at=first_observed_at,
        ongoing_sessions=ongoing,
        buckets=buckets,
        totals=totals,
    )
    return conditional_response(
        request,
        result.model_dump_json(by_alias=True).encode(),
        ttl=30,
        validator_body=result.model_dump_json(by_alias=True, exclude={"as_of"}).encode(),
    )
